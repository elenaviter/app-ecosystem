"""Select one immutable Project Board source for both the command and relay."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import sys
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import utc_now
from .source_composite import prepare_client_release
from .relay_source import (
    CLIENT_SOURCE_PATHS,
    SELECTION_SCHEMA,
    activation_lock,
    canonical_release_version,
    client_release_root,
    client_source_root,
    clear_selection,
    describe_source,
    read_selection,
    released_selection,
    snapshot_selection,
    write_selection,
)
from .release_install import (
    ReleaseInstallError,
    activate_installed_release,
    active_pb,
    active_python,
    active_release_id,
    active_release_path,
    default_release_root,
    install_launcher,
    install_release_environment,
    installed_environment,
    launcher_status,
    prune_installed_releases,
    published_release_id,
    validate_launcher,
)
from .source_manifest import (
    CLIENT_SOURCE_PATHS as INSTALL_SOURCE_PATHS,
    component_records,
    normalise_components,
)


def installed_release_source() -> dict[str, Any]:
    """The immutable host release that owns the running ``pb`` command."""

    try:
        distribution = metadata.distribution("project-board")
    except metadata.PackageNotFoundError as exc:
        raise DomainError(
            "work_client_release_not_installed",
            "Install the released project-board package before selecting a host source.",
        ) from exc
    entrypoint = Path(
        distribution.locate_file("project_board/client/entrypoint.py")
    ).resolve()
    source = describe_source(entrypoint, scope_paths=CLIENT_SOURCE_PATHS)
    if source.get("mode") not in {"released", "snapshot"}:
        raise DomainError(
            "work_client_release_bootstrap_required",
            "Source selection must run from an installed Project Board release environment.",
            details={"observed_source": source},
        )
    return source


def effective_selection(
    root: Path, *, release_source: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The explicit selector, or the installed release before first selection."""

    selected = read_selection(root)
    if selected:
        return selected
    installed = dict(release_source or installed_release_source())
    mode = str(installed.get("mode") or "")
    if mode == "released":
        installed["version"] = canonical_release_version(installed.get("version"))
    elif mode != "snapshot":
        raise DomainError(
            "work_client_release_bootstrap_required",
            "The running Project Board command has no installed release identity.",
            details={"observed_source": installed},
        )
    installed["schema"] = SELECTION_SCHEMA
    installed["implicit"] = True
    return installed


def source_matches(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    mode = str(expected.get("mode") or "")
    if str(observed.get("mode") or "") != mode:
        return False
    if mode == "released":
        try:
            return canonical_release_version(
                observed.get("version")
            ) == canonical_release_version(expected.get("version"))
        except DomainError:
            return False
    if mode == "snapshot":
        expected_release_id = str(expected.get("release_id") or "")
        if expected_release_id:
            if str(observed.get("release_id") or "") != expected_release_id:
                return False
            observed_raw = observed.get("components")
            expected_raw = expected.get("components")
            if not observed_raw and not expected_raw:
                return True
            if not observed_raw or not expected_raw:
                return False
            try:
                observed_components = component_records(
                    normalise_components(observed_raw),
                    include_repository=False,
                )
                expected_components = component_records(
                    normalise_components(expected_raw),
                    include_repository=False,
                )
            except (TypeError, ValueError):
                return False
            return observed_components == expected_components
        return (
            str(observed.get("commit") or "") == str(expected.get("commit") or "")
            and dict(observed.get("subtrees") or {})
            == dict(expected.get("subtrees") or {})
        )
    return False


class ClientSourceController:
    """Transactional source changes around one target's supervised relay."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        service: Any | None = None,
        release_source: Mapping[str, Any] | None = None,
        release_root: str | Path | None = None,
        base_python: str | Path | None = None,
        launcher: str | Path | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.selection_root = client_source_root(self.config_path)
        self.root = (
            Path(release_root).expanduser().resolve()
            if release_root is not None
            else client_release_root(self.config_path)
        )
        self._release_source = dict(release_source or installed_release_source())
        self.base_python = Path(base_python or sys.executable).expanduser().resolve()
        if launcher is not None:
            self.launcher = Path(launcher).expanduser()
        elif self.root == default_release_root():
            self.launcher = Path.home() / ".local" / "bin" / "pb"
        else:
            self.launcher = self.root / "bin" / "pb"
        if service is None:
            from .relay_service import RelayService

            service = RelayService.create(self.config_path)
        self.service = service

    def status(self) -> dict[str, Any]:
        selected = effective_selection(
            self.selection_root, release_source=self._release_source
        )
        actual_release = dict(self._release_source)
        active_id = active_release_id(self.root)
        active_path = active_release_path(self.root)
        active_installation = (
            installed_environment(active_path) if active_path is not None else {}
        )
        return {
            "schema": "project-board.client-source-status.v2",
            "root": str(self.root),
            "selection_root": str(self.selection_root),
            "selected": selected,
            "running_release": actual_release,
            "released_bootstrap": actual_release,
            "selection_matches_bootstrap": source_matches(actual_release, selected),
            "active_release": {
                "release_id": active_id,
                "path": str(active_path) if active_path is not None else "",
                "environment": dict(active_installation.get("environment") or {}),
                "source": dict(active_installation.get("source") or {}),
            },
            "environment": {
                "python": str(active_python(self.root)),
                "pb": str(active_pb(self.root)),
            },
            "launcher": launcher_status(
                self.launcher,
                expected_pb=active_pb(self.root),
            ),
            "relay": self.service.status(),
        }

    def use_code(
        self,
        *,
        repository: str | Path,
        ref: str,
        expect: str,
        kdcube_repository: str | Path,
        kdcube_ref: str,
        expect_kdcube: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        repo = Path(repository).expanduser().resolve()
        kdcube_repo = Path(kdcube_repository).expanduser().resolve()
        self._preflight_service()
        with activation_lock(self.root):
            previous = effective_selection(
                self.selection_root, release_source=self._release_source
            )
            previous_explicit = read_selection(self.selection_root)
            previous_release_id = active_release_id(self.root)
            release, evidence = prepare_client_release(
                app_ecosystem_repository=repo,
                app_ecosystem_ref=ref,
                expect_app_ecosystem=expect,
                kdcube_repository=kdcube_repo,
                kdcube_ref=kdcube_ref,
                expect_kdcube=expect_kdcube,
                root=self.root,
            )
            selected = snapshot_selection(release)
            installation = self._install_release(
                release_id=release.release_id,
                requirements=tuple(
                    release.path / relative for relative in INSTALL_SOURCE_PATHS
                ),
                source=selected,
            )
            receipt = {
                "schema": "project-board.client-source-activation.v1",
                "selected": selected,
                "previous": previous,
                "installation": installation,
                "repositories": {
                    "app_ecosystem": str(repo),
                    "kdcube": str(kdcube_repo),
                },
                "checkouts": evidence,
                # Compatibility fields name the App Ecosystem side of the
                # composite source for older receipt readers.
                "repository": str(repo),
                "checkout": evidence["app_ecosystem"],
            }
            return self._activate_restart_and_verify(
                selected=selected,
                previous=previous,
                previous_explicit=previous_explicit,
                release_id=release.release_id,
                previous_release_id=previous_release_id,
                wait_seconds=wait_seconds,
                receipt=receipt,
            )

    def use_release(
        self, *, expect_version: str, wait_seconds: float
    ) -> dict[str, Any]:
        expected = canonical_release_version(expect_version)
        self._preflight_service()
        with activation_lock(self.root):
            previous = effective_selection(
                self.selection_root, release_source=self._release_source
            )
            previous_explicit = read_selection(self.selection_root)
            previous_release_id = active_release_id(self.root)
            selected = released_selection(expected)
            release_id = published_release_id(expected)
            installation = self._install_release(
                release_id=release_id,
                requirements=(f"project-board=={expected}",),
                source=selected,
                expected_project_board_version=expected,
            )
            receipt = {
                "schema": "project-board.client-source-activation.v1",
                "selected": selected,
                "previous": previous,
                "installation": installation,
            }
            return self._activate_restart_and_verify(
                selected=selected,
                previous=previous,
                previous_explicit=previous_explicit,
                release_id=release_id,
                previous_release_id=previous_release_id,
                wait_seconds=wait_seconds,
                receipt=receipt,
            )

    def _install_release(
        self,
        *,
        release_id: str,
        requirements: tuple[str | Path, ...],
        source: Mapping[str, Any],
        expected_project_board_version: str = "",
    ) -> dict[str, Any]:
        try:
            return install_release_environment(
                root=self.root,
                release_id=release_id,
                requirements=requirements,
                source=source,
                base_python=self.base_python,
                expected_project_board_version=expected_project_board_version,
            )
        except ReleaseInstallError as exc:
            raise DomainError(exc.code, str(exc), details=exc.details) from exc

    def _preflight_service(self) -> None:
        if (
            self.service.definition_path.exists()
            and not self.service.definition_uses_bootstrap()
        ):
            raise DomainError(
                "work_client_source_service_definition_stale",
                "The installed relay definition does not use the released "
                "Project Board bootstrap. Run pb relay-service install before "
                "changing source.",
                details={"definition": str(self.service.definition_path)},
            )

    def _activate_restart_and_verify(
        self,
        *,
        selected: Mapping[str, Any],
        previous: Mapping[str, Any],
        previous_explicit: Mapping[str, Any],
        release_id: str,
        previous_release_id: str,
        wait_seconds: float,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            validate_launcher(
                self.launcher,
                expected_pb=active_pb(self.root),
            )
        except ReleaseInstallError as exc:
            raise DomainError(exc.code, str(exc), details=exc.details) from exc
        try:
            activate_installed_release(self.root, release_id)
            selected = write_selection(self.selection_root, selected)
            install_launcher(
                self.launcher,
                expected_pb=active_pb(self.root),
            )
        except Exception as exc:
            rollback: dict[str, Any] = {"state": "restored"}
            try:
                self._restore(previous_explicit, previous_release_id)
            except Exception as restore_exc:
                rollback = {
                    "state": "restore_failed",
                    "reason": str(getattr(restore_exc, "code", "") or restore_exc),
                }
            details: dict[str, Any] = {
                "release_id": release_id,
                "reason": str(getattr(exc, "code", "") or exc),
                "rollback": rollback,
            }
            if isinstance(exc, DomainError):
                details["cause"] = exc.to_dict()
            elif isinstance(exc, ReleaseInstallError):
                details["cause"] = {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                }
            raise DomainError(
                "work_client_source_activation_failed",
                "The candidate Project Board release could not be activated; the previous release remains selected.",
                details=details,
            ) from exc
        receipt["selected"] = selected
        if not self.service.definition_path.exists():
            return {
                **receipt,
                "state": "selected",
                "relay_installed": False,
                "relay_restart_required": True,
                "pruned_releases": prune_installed_releases(
                    self.root,
                    keep=(release_id, previous_release_id),
                ),
            }
        since = utc_now()
        try:
            self.service.restart()
            startup = self.service.await_source(
                selected, since=since, wait_seconds=wait_seconds
            )
        except Exception as exc:
            rollback = self._restore_and_restart(
                previous_explicit,
                previous_release_id,
                previous,
                wait_seconds,
            )
            details: dict[str, Any] = {
                "selected": dict(selected),
                "reason": str(getattr(exc, "code", "") or exc),
                "rollback": rollback,
            }
            if isinstance(exc, DomainError):
                details["cause"] = exc.to_dict()
                for key in ("command", "returncode", "stderr"):
                    if key in exc.details:
                        details[key] = exc.details[key]
            raise DomainError(
                "work_client_source_activation_failed",
                "The relay could not restart on the selected Project Board "
                "source; the previous source was restored.",
                details=details,
            ) from exc
        if startup.get("state") != "started":
            rollback = self._restore_and_restart(
                previous_explicit,
                previous_release_id,
                previous,
                wait_seconds,
            )
            raise DomainError(
                "work_client_source_activation_failed",
                "The restarted relay did not report the selected Project "
                "Board source; the previous source was restored.",
                details={
                    "selected": dict(selected),
                    "startup": startup,
                    "rollback": rollback,
                },
            )

        return {
            **receipt,
            "state": "activated",
            "relay_installed": True,
            "startup": startup,
            "pruned_releases": prune_installed_releases(
                self.root,
                keep=(release_id, previous_release_id),
            ),
        }

    def _restore(
        self,
        previous_explicit: Mapping[str, Any],
        previous_release_id: str,
    ) -> None:
        activate_installed_release(self.root, previous_release_id)
        if previous_explicit:
            restored = dict(previous_explicit)
            restored["selected_at"] = utc_now()
            write_selection(self.selection_root, restored)
        else:
            clear_selection(self.selection_root)

    def _restore_and_restart(
        self,
        previous_explicit: Mapping[str, Any],
        previous_release_id: str,
        previous: Mapping[str, Any],
        wait_seconds: float,
    ) -> dict[str, Any]:
        try:
            self._restore(previous_explicit, previous_release_id)
        except Exception as exc:
            return {
                "state": "restore_failed",
                "source": dict(previous),
                "reason": str(getattr(exc, "code", "") or exc),
            }
        since = utc_now()
        try:
            self.service.restart()
            startup = self.service.await_source(
                previous, since=since, wait_seconds=wait_seconds
            )
            if startup.get("state") != "started":
                return {
                    "state": "restore_failed",
                    "source": dict(previous),
                    "startup": startup,
                }
            return {"state": "restored", "source": dict(previous), "startup": startup}
        except Exception as exc:
            return {
                "state": "restore_failed",
                "source": dict(previous),
                "reason": str(getattr(exc, "code", "") or exc),
            }


__all__ = [
    "ClientSourceController",
    "effective_selection",
    "installed_release_source",
    "source_matches",
]

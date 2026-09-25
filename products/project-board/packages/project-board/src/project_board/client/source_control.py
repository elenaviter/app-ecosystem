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
    canonical_release_version,
    client_release_root,
    client_source_root,
    clear_selection,
    describe_source,
    host_target_config_paths,
    read_selection,
    released_selection,
    snapshot_selection,
    write_selection,
)
from .release_install import (
    DEFAULT_IMPORT_SMOKE,
    LauncherSnapshot,
    ReleaseInstallError,
    activate_installed_release,
    activation_lock,
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
    release_path,
    restore_launcher,
    snapshot_launcher,
    validate_launcher,
)
from .source_manifest import (
    CLIENT_SOURCE_IMPORTS,
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
    """Transactional source changes across every target on one worker host."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        service: Any | None = None,
        release_source: Mapping[str, Any] | None = None,
        release_root: str | Path | None = None,
        base_python: str | Path | None = None,
        launcher: str | Path | None = None,
        host_services: Mapping[str | Path, Any] | None = None,
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
        self.services = self._resolve_host_services(host_services)

    def _resolve_host_services(
        self, declared: Mapping[str | Path, Any] | None
    ) -> dict[Path, Any]:
        services = {
            Path(path).expanduser().resolve(): candidate
            for path, candidate in dict(declared or {}).items()
        }
        services[self.config_path] = self.service
        if declared is not None:
            return dict(sorted(services.items(), key=lambda item: str(item[0])))
        from .relay_service import RelayService

        for path in host_target_config_paths(self.config_path):
            if path not in services:
                services[path] = RelayService.create(path)
        return dict(sorted(services.items(), key=lambda item: str(item[0])))

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
            "host_relays": [
                {"config": str(config), **candidate.status()}
                for config, candidate in self.services.items()
            ],
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
        installed_services = self._preflight_services()
        with activation_lock(self.root):
            previous = effective_selection(
                self.selection_root, release_source=self._release_source
            )
            previous_explicit = self._selection_snapshots()
            previous_release_id = active_release_id(self.root)
            previous_host_source = self._active_source(previous_release_id)
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
                smoke_imports=(*CLIENT_SOURCE_IMPORTS, *DEFAULT_IMPORT_SMOKE),
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
                previous_explicit=previous_explicit,
                release_id=release.release_id,
                previous_release_id=previous_release_id,
                previous_host_source=previous_host_source,
                installed_services=installed_services,
                wait_seconds=wait_seconds,
                receipt=receipt,
            )

    def use_release(
        self, *, expect_version: str, wait_seconds: float
    ) -> dict[str, Any]:
        expected = canonical_release_version(expect_version)
        installed_services = self._preflight_services()
        with activation_lock(self.root):
            previous = effective_selection(
                self.selection_root, release_source=self._release_source
            )
            previous_explicit = self._selection_snapshots()
            previous_release_id = active_release_id(self.root)
            previous_host_source = self._active_source(previous_release_id)
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
                previous_explicit=previous_explicit,
                release_id=release_id,
                previous_release_id=previous_release_id,
                previous_host_source=previous_host_source,
                installed_services=installed_services,
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
        smoke_imports: tuple[str, ...] = DEFAULT_IMPORT_SMOKE,
    ) -> dict[str, Any]:
        try:
            return install_release_environment(
                root=self.root,
                release_id=release_id,
                requirements=requirements,
                source=source,
                base_python=self.base_python,
                smoke_imports=smoke_imports,
                expected_project_board_version=expected_project_board_version,
            )
        except ReleaseInstallError as exc:
            raise DomainError(exc.code, str(exc), details=exc.details) from exc

    def _preflight_services(self) -> list[tuple[Path, Any]]:
        installed: list[tuple[Path, Any]] = []
        stale: list[dict[str, str]] = []
        for config, service in self.services.items():
            if not service.definition_path.exists():
                continue
            installed.append((config, service))
            if not service.definition_uses_bootstrap():
                stale.append(
                    {
                        "config": str(config),
                        "definition": str(service.definition_path),
                    }
                )
        if stale:
            raise DomainError(
                "work_client_source_service_definition_stale",
                "Every installed relay must use the host's current Project Board "
                "release before changing source. Run pb relay-service install "
                "for the listed targets.",
                details={"relays": stale},
            )
        return installed

    def _selection_snapshots(self) -> dict[Path, dict[str, Any]]:
        return {
            config: read_selection(client_source_root(config))
            for config in self.services
        }

    def _active_source(self, release_id: str) -> dict[str, Any]:
        if release_id:
            installation = installed_environment(release_path(self.root, release_id))
            source = installation.get("source")
            if isinstance(source, dict) and source.get("mode") in {
                "released",
                "snapshot",
            }:
                return dict(source)
        return dict(self._release_source)

    def _activate_restart_and_verify(
        self,
        *,
        selected: Mapping[str, Any],
        previous_explicit: Mapping[Path, Mapping[str, Any]],
        release_id: str,
        previous_release_id: str,
        previous_host_source: Mapping[str, Any],
        installed_services: list[tuple[Path, Any]],
        wait_seconds: float,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            validate_launcher(
                self.launcher,
                expected_pb=active_pb(self.root),
            )
            launcher_snapshot = snapshot_launcher(self.launcher)
        except ReleaseInstallError as exc:
            raise DomainError(exc.code, str(exc), details=exc.details) from exc

        mutation_started = False
        try:
            self._stop_services(installed_services)
            mutation_started = True
            activate_installed_release(self.root, release_id)
            target_selections = self._write_host_selections(selected)
            install_launcher(
                self.launcher,
                expected_pb=active_pb(self.root),
            )
        except Exception as exc:
            if mutation_started:
                rollback = self._restore_and_restart(
                    previous_explicit=previous_explicit,
                    previous_release_id=previous_release_id,
                    previous_host_source=previous_host_source,
                    launcher_snapshot=launcher_snapshot,
                    installed_services=installed_services,
                    wait_seconds=wait_seconds,
                )
            else:
                rollback = self._restart_without_restore(
                    previous_host_source=previous_host_source,
                    installed_services=installed_services,
                    wait_seconds=wait_seconds,
                )
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
        receipt["selected"] = dict(selected)
        receipt["target_selections"] = target_selections
        if not installed_services:
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
        try:
            startups = self._restart_services(
                installed_services,
                expected=selected,
                wait_seconds=wait_seconds,
            )
        except Exception as exc:
            rollback = self._restore_and_restart(
                previous_explicit=previous_explicit,
                previous_release_id=previous_release_id,
                previous_host_source=previous_host_source,
                launcher_snapshot=launcher_snapshot,
                installed_services=installed_services,
                wait_seconds=wait_seconds,
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

        return {
            **receipt,
            "state": "activated",
            "relay_installed": True,
            # Keep the initiating target's startup at the compatibility key.
            "startup": next(
                (
                    item["startup"]
                    for item in startups
                    if item["config"] == str(self.config_path)
                ),
                startups[0]["startup"],
            ),
            "host_relays": startups,
            "pruned_releases": prune_installed_releases(
                self.root,
                keep=(release_id, previous_release_id),
            ),
        }

    def _write_host_selections(
        self, selected: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        written: list[dict[str, Any]] = []
        for config in self.services:
            selection_root = client_source_root(config)
            value = write_selection(selection_root, selected)
            written.append(
                {
                    "config": str(config),
                    "selection_root": str(selection_root),
                    "selected": value,
                }
            )
        return written

    def _stop_services(self, services: list[tuple[Path, Any]]) -> None:
        stopped: list[str] = []
        for config, service in services:
            result = service.stop()
            if result.get("running") is True:
                raise DomainError(
                    "work_client_source_relay_stop_failed",
                    "An installed relay remained running, so the host release "
                    "was not changed.",
                    details={"config": str(config), "stopped": stopped},
                )
            stopped.append(str(config))

    def _restart_services(
        self,
        services: list[tuple[Path, Any]],
        *,
        expected: Mapping[str, Any],
        wait_seconds: float,
    ) -> list[dict[str, Any]]:
        startups: list[dict[str, Any]] = []
        for config, service in services:
            since = utc_now()
            service.restart()
            startup = service.await_source(
                expected, since=since, wait_seconds=wait_seconds
            )
            if startup.get("state") != "started":
                raise DomainError(
                    "work_client_source_relay_start_mismatch",
                    "A restarted relay did not report the selected Project Board "
                    "source.",
                    details={
                        "config": str(config),
                        "selected": dict(expected),
                        "startup": startup,
                    },
                )
            startups.append({"config": str(config), "startup": startup})
        return startups

    def _restore(
        self,
        previous_explicit: Mapping[Path, Mapping[str, Any]],
        previous_release_id: str,
        launcher_snapshot: LauncherSnapshot,
    ) -> None:
        activate_installed_release(self.root, previous_release_id)
        for config, previous in previous_explicit.items():
            selection_root = client_source_root(config)
            if previous:
                write_selection(selection_root, previous)
            else:
                clear_selection(selection_root)
        restore_launcher(launcher_snapshot)

    def _restore_and_restart(
        self,
        *,
        previous_explicit: Mapping[Path, Mapping[str, Any]],
        previous_release_id: str,
        previous_host_source: Mapping[str, Any],
        launcher_snapshot: LauncherSnapshot,
        installed_services: list[tuple[Path, Any]],
        wait_seconds: float,
    ) -> dict[str, Any]:
        stop_failures: list[dict[str, str]] = []
        for config, service in installed_services:
            try:
                result = service.stop()
                if result.get("running") is True:
                    stop_failures.append(
                        {
                            "config": str(config),
                            "reason": "relay_remained_running",
                        }
                    )
            except Exception as exc:
                stop_failures.append(
                    {
                        "config": str(config),
                        "reason": str(getattr(exc, "code", "") or exc),
                    }
                )
        if stop_failures:
            return {
                "state": "restore_failed",
                "source": dict(previous_host_source),
                "reason": "candidate_relays_could_not_be_stopped",
                "stop_failures": stop_failures,
            }
        try:
            self._restore(
                previous_explicit,
                previous_release_id,
                launcher_snapshot,
            )
        except Exception as exc:
            return {
                "state": "restore_failed",
                "source": dict(previous_host_source),
                "reason": str(getattr(exc, "code", "") or exc),
            }
        try:
            startups = self._restart_services(
                installed_services,
                expected=previous_host_source,
                wait_seconds=wait_seconds,
            )
            return {
                "state": "restored",
                "source": dict(previous_host_source),
                "host_relays": startups,
            }
        except Exception as exc:
            return {
                "state": "restore_failed",
                "source": dict(previous_host_source),
                "reason": str(getattr(exc, "code", "") or exc),
            }

    def _restart_without_restore(
        self,
        *,
        previous_host_source: Mapping[str, Any],
        installed_services: list[tuple[Path, Any]],
        wait_seconds: float,
    ) -> dict[str, Any]:
        try:
            startups = self._restart_services(
                installed_services,
                expected=previous_host_source,
                wait_seconds=wait_seconds,
            )
        except Exception as exc:
            return {
                "state": "restore_failed",
                "source": dict(previous_host_source),
                "reason": str(getattr(exc, "code", "") or exc),
            }
        return {
            "state": "restored",
            "source": dict(previous_host_source),
            "host_relays": startups,
        }


__all__ = [
    "ClientSourceController",
    "effective_selection",
    "installed_release_source",
    "source_matches",
]

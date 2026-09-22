"""Select one immutable Project Board source for both the command and relay."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import utc_now
from .relay_source import (
    CLIENT_SOURCE_PATHS,
    PROJECT_BOARD_CODE_ENTRYPOINT,
    activate_release,
    activation_lock,
    canonical_release_version,
    client_source_root,
    current_release,
    describe_source,
    prepare_release,
    prune_releases,
    read_selection,
    released_selection,
    selected_release,
    snapshot_selection,
    write_selection,
)


def installed_release_source() -> dict[str, Any]:
    """The immutable distribution that owns the stable ``pb`` bootstrap."""

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
    if source.get("mode") != "released":
        raise DomainError(
            "work_client_release_bootstrap_required",
            "Source selection must run from the released project-board command.",
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
    released = dict(release_source or installed_release_source())
    return {
        "schema": "project-board.client-source-selection.v1",
        "mode": "released",
        "version": canonical_release_version(released.get("version")),
        "implicit": True,
    }


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
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.root = client_source_root(self.config_path)
        self._release_source = dict(release_source or installed_release_source())
        if service is None:
            from .relay_service import RelayService

            service = RelayService.create(self.config_path)
        self.service = service

    def status(self) -> dict[str, Any]:
        selected = effective_selection(
            self.root, release_source=self._release_source
        )
        actual_release = dict(self._release_source)
        return {
            "schema": "project-board.client-source-status.v1",
            "root": str(self.root),
            "selected": selected,
            "released_bootstrap": actual_release,
            "selection_matches_bootstrap": (
                selected.get("mode") != "released"
                or source_matches(actual_release, selected)
            ),
            "relay": self.service.status(),
        }

    def use_code(
        self,
        *,
        repository: str | Path,
        ref: str,
        expect: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        repo = Path(repository).expanduser().resolve()
        approved = str(expect or "").strip().lower()
        if len(approved) != 40 or any(
            char not in "0123456789abcdef" for char in approved
        ):
            raise DomainError(
                "work_client_source_expected_commit_invalid",
                "Code source selection requires the full approved 40-character commit.",
                details={"expected": approved},
            )
        self._preflight_service()
        with activation_lock(self.root):
            previous = effective_selection(
                self.root, release_source=self._release_source
            )
            previous_release = current_release(self.root)
            release, evidence = prepare_release(
                repository=repo,
                ref=ref,
                root=self.root,
                entrypoint_path=PROJECT_BOARD_CODE_ENTRYPOINT,
                source_paths=CLIENT_SOURCE_PATHS,
                expect=approved,
            )
            selected = write_selection(self.root, snapshot_selection(release))
            # ``selection.json`` is authoritative and atomic. The link is
            # retained for W202 readers, after the source decision is durable.
            activate_release(self.root, release)
            receipt = {
                "schema": "project-board.client-source-activation.v1",
                "selected": selected,
                "previous": previous,
                "repository": str(repo),
                "checkout": evidence,
            }
            return self._restart_and_verify(
                selected=selected,
                previous=previous,
                previous_release=previous_release,
                wait_seconds=wait_seconds,
                receipt=receipt,
            )

    def use_release(
        self, *, expect_version: str, wait_seconds: float
    ) -> dict[str, Any]:
        installed = canonical_release_version(self._release_source.get("version"))
        expected = canonical_release_version(expect_version)
        if expected != installed:
            raise DomainError(
                "work_client_release_version_unexpected",
                f"The installed project-board version is {installed}, not the approved {expected}.",
                details={"installed": installed, "expected": expected},
            )
        self._preflight_service()
        with activation_lock(self.root):
            previous = effective_selection(
                self.root, release_source=self._release_source
            )
            previous_release = current_release(self.root)
            selected = write_selection(self.root, released_selection(installed))
            receipt = {
                "schema": "project-board.client-source-activation.v1",
                "selected": selected,
                "previous": previous,
            }
            return self._restart_and_verify(
                selected=selected,
                previous=previous,
                previous_release=previous_release,
                wait_seconds=wait_seconds,
                receipt=receipt,
            )

    def _preflight_service(self) -> None:
        if (
            self.service.definition_path.exists()
            and not self.service.definition_uses_bootstrap()
        ):
            raise DomainError(
                "work_client_source_service_definition_stale",
                "The installed relay definition does not use the released Project Board bootstrap. Run pb relay-service install before changing source.",
                details={"definition": str(self.service.definition_path)},
            )

    def _restart_and_verify(
        self,
        *,
        selected: Mapping[str, Any],
        previous: Mapping[str, Any],
        previous_release: Any,
        wait_seconds: float,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.service.definition_path.exists():
            return {
                **receipt,
                "state": "selected",
                "relay_installed": False,
                "relay_restart_required": True,
            }
        since = utc_now()
        try:
            self.service.restart()
            startup = self.service.await_source(
                selected, since=since, wait_seconds=wait_seconds
            )
        except Exception as exc:
            rollback = self._restore_and_restart(previous, previous_release, wait_seconds)
            raise DomainError(
                "work_client_source_activation_failed",
                "The relay could not restart on the selected Project Board source; the previous source was restored.",
                details={
                    "selected": dict(selected),
                    "reason": str(getattr(exc, "code", "") or exc),
                    "rollback": rollback,
                },
            ) from exc
        if startup.get("state") != "started":
            rollback = self._restore_and_restart(previous, previous_release, wait_seconds)
            raise DomainError(
                "work_client_source_activation_failed",
                "The restarted relay did not report the selected Project Board source; the previous source was restored.",
                details={
                    "selected": dict(selected),
                    "startup": startup,
                    "rollback": rollback,
                },
            )

        keep = [str(selected.get("commit") or "")]
        if previous.get("mode") == "snapshot":
            keep.append(str(previous.get("commit") or ""))
        return {
            **receipt,
            "state": "activated",
            "relay_installed": True,
            "startup": startup,
            "pruned_releases": prune_releases(
                self.root, tuple(commit for commit in keep if commit)
            ),
        }

    def _restore(self, previous: Mapping[str, Any], previous_release: Any) -> None:
        restored = dict(previous)
        restored.pop("implicit", None)
        restored["selected_at"] = utc_now()
        write_selection(self.root, restored)
        if previous.get("mode") == "snapshot":
            release = previous_release or selected_release(self.root, previous)
            activate_release(self.root, release)

    def _restore_and_restart(
        self,
        previous: Mapping[str, Any],
        previous_release: Any,
        wait_seconds: float,
    ) -> dict[str, Any]:
        self._restore(previous, previous_release)
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

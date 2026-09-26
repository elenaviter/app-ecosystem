"""Build one immutable Project Board client release from its source repositories.

Since W322 Step 1 the client is App Ecosystem only (``CLIENT_COMPONENTS``); a
future source is another component, not another parameter.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from ..contract.errors import DomainError
from .io import atomic_write_json, utc_now
from .relay_source import (
    PROJECT_BOARD_CODE_ENTRYPOINT,
    RELEASE_MARKER,
    RELEASE_SCHEMA,
    RELEASES_DIR,
    RelaySourceRelease,
    _expected_blobs,
    _extract_archive,
    _subtree_ids,
    checkout_evidence,
    read_release,
    resolve_commit,
    verify_release_files,
)
from .source_manifest import (
    APP_ECOSYSTEM_COMPONENT,
    CLIENT_COMPONENTS,
    CLIENT_SOURCE_PATHS,
    SOURCE_PATHS_BY_COMPONENT,
    SourceComponent,
    normalise_components,
    release_id_for_components,
)


def _export_component(
    *, name: str, repository: Path, ref: str
) -> tuple[SourceComponent, dict[str, str]]:
    paths = SOURCE_PATHS_BY_COMPONENT[name]
    commit = resolve_commit(repository, ref)
    return (
        SourceComponent(
            name=name,
            commit=commit,
            subtrees=_subtree_ids(repository, commit, paths),
            repository=str(repository),
        ),
        _expected_blobs(repository, commit, paths),
    )


def export_client_release(
    *,
    app_ecosystem_repository: Path,
    app_ecosystem_ref: str,
    root: Path,
    between: Callable[[], None] | None = None,
) -> RelaySourceRelease:
    """Export the complete client as one verified release."""

    repositories = {APP_ECOSYSTEM_COMPONENT: Path(app_ecosystem_repository).resolve()}
    refs = {APP_ECOSYSTEM_COMPONENT: app_ecosystem_ref}
    components: list[SourceComponent] = []
    expected: dict[str, str] = {}
    for name in CLIENT_COMPONENTS:
        component, blobs = _export_component(
            name=name, repository=repositories[name], ref=refs[name]
        )
        collisions = sorted(set(expected).intersection(blobs))
        if collisions:
            raise DomainError(
                "work_client_source_path_collision",
                "Client source repositories export the same file path.",
                details={"component": name, "paths": collisions[:20]},
            )
        components.append(component)
        expected.update(blobs)
    canonical_components = normalise_components(components)
    release_id = release_id_for_components(canonical_components)
    if between is not None:
        between()

    releases = Path(root) / RELEASES_DIR
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = releases / release_id
    existing = read_release(final)
    if (
        existing is not None
        and existing.schema == RELEASE_SCHEMA
        and existing.release_id == release_id
        and not verify_release_files(final, expected)
    ):
        return existing
    if final.exists():
        shutil.rmtree(final)

    stage = releases / f".{release_id}.tmp-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    try:
        for component in canonical_components:
            _extract_archive(
                repositories[component.name],
                component.commit,
                SOURCE_PATHS_BY_COMPONENT[component.name],
                stage,
            )
        mismatched = verify_release_files(stage, expected)
        if mismatched:
            raise DomainError(
                "work_relay_source_export_mismatch",
                f"{len(mismatched)} exported file(s) do not match the "
                "client source manifest.",
                details={"release_id": release_id, "paths": mismatched[:20]},
            )
        release = RelaySourceRelease(
            release_id=release_id,
            path=final,
            entrypoint_path=PROJECT_BOARD_CODE_ENTRYPOINT,
            source_paths=CLIENT_SOURCE_PATHS,
            components=canonical_components,
            exported_at=utc_now(),
        )
        if not (stage / PROJECT_BOARD_CODE_ENTRYPOINT).is_file():
            raise DomainError(
                "work_relay_source_entrypoint_missing",
                "The Project Board code entry point is not in the App "
                "Ecosystem commit.",
                details={
                    "release_id": release_id,
                    "entrypoint_path": PROJECT_BOARD_CODE_ENTRYPOINT,
                },
            )
        atomic_write_json(stage / RELEASE_MARKER, release.marker())
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return release


def _approved_full_commit(value: str, *, component: str) -> str:
    approved = str(value or "").strip().lower()
    if len(approved) != 40 or any(
        character not in "0123456789abcdef" for character in approved
    ):
        raise DomainError(
            "work_client_source_expected_commit_invalid",
            f"The approved {component} commit must be the full 40-character id.",
            details={"component": component, "expected": approved},
        )
    return approved


def prepare_client_release(
    *,
    app_ecosystem_repository: Path,
    app_ecosystem_ref: str,
    expect_app_ecosystem: str,
    root: Path,
    between: Callable[[], None] | None = None,
) -> tuple[RelaySourceRelease, dict[str, dict[str, Any]]]:
    """Pin, approve, observe and export every repository in the client source."""

    repositories = {APP_ECOSYSTEM_COMPONENT: Path(app_ecosystem_repository).resolve()}
    refs = {APP_ECOSYSTEM_COMPONENT: str(app_ecosystem_ref)}
    approved = {
        APP_ECOSYSTEM_COMPONENT: _approved_full_commit(
            expect_app_ecosystem, component=APP_ECOSYSTEM_COMPONENT
        ),
    }
    resolved: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for name in CLIENT_COMPONENTS:
        commit = resolve_commit(repositories[name], refs[name])
        if commit != approved[name]:
            raise DomainError(
                "work_relay_source_commit_unexpected",
                f"The {name} ref resolves to {commit[:12]}, not the approved "
                f"{approved[name][:12]}. Nothing was changed.",
                details={
                    "component": name,
                    "ref": refs[name],
                    "resolved": commit,
                    "expected": approved[name],
                },
            )
        resolved[name] = commit
        evidence[name] = checkout_evidence(
            repositories[name], *SOURCE_PATHS_BY_COMPONENT[name]
        )
    release = export_client_release(
        app_ecosystem_repository=repositories[APP_ECOSYSTEM_COMPONENT],
        app_ecosystem_ref=resolved[APP_ECOSYSTEM_COMPONENT],
        root=root,
        between=between,
    )
    return release, evidence


__all__ = ["export_client_release", "prepare_client_release"]

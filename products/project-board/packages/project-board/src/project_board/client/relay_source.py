"""A content-addressed client source exported and selected as one manifest.

The relay LaunchAgent used to run ``tools/problem_board.py`` straight out of
the shared checkout, so a restart loaded whatever the working tree held at
that instant, including another worker's half-typed edit. Here the input is a
manifest of full commits. ``git archive`` reads each object store, so the state of
the working tree cannot reach the export, and the exported files from every
repository are verified blob by blob against ``git ls-tree`` before the
release is named.
The atomic ``selection.json`` record is the authority shared by the command
and relay. A ``current`` symlink is maintained only for readers predating that
selector and never supplies an implicit client selection.

Layout under the host's relay-source root::

    releases/<release-id>/<repo-relative paths>  export plus release.json
    selection.json                               released version or manifest
    current -> releases/<release-id>             compatibility pointer
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from packaging.version import InvalidVersion, Version

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json, utc_now
from .source_manifest import (
    APP_ECOSYSTEM_COMPONENT,
    APP_ECOSYSTEM_SOURCE_PATHS,
    CLIENT_SOURCE_PATHS,
    KDCUBE_COMPONENT,
    SourceComponent,
    component_named,
    component_records,
    normalise_components,
    release_id_for_components,
    validate_release_id,
)

RELEASE_SCHEMA = "project-board.client-source-release.v3"
SINGLE_REPOSITORY_RELEASE_SCHEMA = "project-board.client-source-release.v2"
LEGACY_RELEASE_SCHEMA = "problem-board.relay-source-release.v1"
RELEASE_MARKER = "release.json"
RELEASES_DIR = "releases"
CURRENT_LINK = "current"
SELECTION_SCHEMA = "project-board.client-source-selection.v2"
LEGACY_SELECTION_SCHEMA = "project-board.client-source-selection.v1"
SELECTION_FILE = "selection.json"
SOURCE_ROOT_DIR = "client-source"
ENTRYPOINT = "problem_board.py"
GIT_TIMEOUT_SECONDS = 120
# How far above the entrypoint a release marker may sit. The export keeps
# repo-relative paths, so the marker is a few directories up from tools/.
MARKER_SEARCH_DEPTH = 8

PROJECT_BOARD_PACKAGE = APP_ECOSYSTEM_SOURCE_PATHS[0]
PROJECT_BOARD_CODE_ENTRYPOINT = (
    f"{PROJECT_BOARD_PACKAGE}/src/project_board/client/code_entrypoint.py"
)


@dataclass(frozen=True, slots=True)
class RelaySourceRelease:
    release_id: str
    path: Path
    entrypoint_path: str
    source_paths: tuple[str, ...]
    components: tuple[SourceComponent, ...]
    exported_at: str
    schema: str = RELEASE_SCHEMA
    tools_path: str = ""
    app_path: str = ""

    @property
    def script(self) -> Path:
        return self.path / self.entrypoint_path

    @property
    def app_ecosystem(self) -> SourceComponent:
        return component_named(self.components, APP_ECOSYSTEM_COMPONENT)

    @property
    def commit(self) -> str:
        """Compatibility name for the App Ecosystem commit."""

        return self.app_ecosystem.commit

    @property
    def subtrees(self) -> dict[str, str]:
        """Compatibility name for the App Ecosystem package trees."""

        return dict(self.app_ecosystem.subtrees)

    @property
    def repository(self) -> str:
        """Compatibility name for the App Ecosystem repository."""

        return self.app_ecosystem.repository

    def marker(self) -> dict[str, Any]:
        if self.schema != RELEASE_SCHEMA:
            return {
                "schema": self.schema,
                "commit": self.commit,
                "entrypoint_path": self.entrypoint_path,
                "source_paths": list(self.source_paths),
                "tools_path": self.tools_path,
                "app_path": self.app_path,
                "subtrees": dict(self.subtrees),
                "repository": self.repository,
                "exported_at": self.exported_at,
            }
        return {
            "schema": RELEASE_SCHEMA,
            "release_id": self.release_id,
            "components": component_records(self.components),
            # These App Ecosystem fields keep status consumers readable while
            # the composite fields remain the authority.
            "commit": self.commit,
            "entrypoint_path": self.entrypoint_path,
            "source_paths": list(self.source_paths),
            "tools_path": self.tools_path,
            "app_path": self.app_path,
            "subtrees": dict(self.subtrees),
            "repository": self.repository,
            "exported_at": self.exported_at,
        }


def client_source_root(config_path: Path) -> Path:
    """The per-target source store shared by the command and relay.

    The former ``relay-source`` directory is deliberately not adopted. Its
    snapshots contain only the old relay entrypoint and application modules,
    so treating its ``current`` link as a client selection would omit the
    packaged command and its Connection Hub dependencies.
    """

    return Path(config_path).parent / SOURCE_ROOT_DIR


def relay_source_root(config_path: Path) -> Path:
    """Compatibility name for callers written before the source became shared."""

    return client_source_root(config_path)


def _git(repository: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise DomainError(
            "work_relay_source_git_missing",
            "git is required to export a relay source release.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DomainError(
            "work_relay_source_git_timeout",
            f"git {args[0]} did not finish within {GIT_TIMEOUT_SECONDS} seconds.",
            details={"repository": str(repository), "command": list(args[:2])},
        ) from exc
    if completed.returncode != 0:
        raise DomainError(
            "work_relay_source_git_failed",
            f"git {args[0]} failed in {repository}.",
            details={
                "repository": str(repository),
                "command": list(args[:3]),
                "stderr": (completed.stderr or "").strip()[:500],
            },
        )
    return completed.stdout


def repository_root(path: Path) -> Path:
    """The work tree that holds ``path``, or a refusal naming the path."""

    try:
        top = _git(Path(path), "rev-parse", "--show-toplevel").strip()
    except DomainError as exc:
        raise DomainError(
            "work_relay_source_not_a_repository",
            f"{path} is not inside a git work tree.",
            details={"path": str(path)},
        ) from exc
    return Path(top).resolve()


def resolve_commit(repository: Path, ref: str) -> str:
    """The commit ``ref`` names, as a full sha. A branch or tag is pinned here."""

    clean = str(ref or "").strip()
    if not clean or clean.startswith("-"):
        raise DomainError(
            "work_relay_source_ref_invalid",
            "A commit, tag or branch name is required.",
            details={"ref": clean},
        )
    try:
        return _git(repository, "rev-parse", "--verify", "--quiet", f"{clean}^{{commit}}").strip()
    except DomainError as exc:
        raise DomainError(
            "work_relay_source_ref_unknown",
            f"{clean} does not name a commit in {repository}.",
            details={"ref": clean, "repository": str(repository)},
        ) from exc


def checkout_evidence(repository: Path, *paths: str) -> dict[str, Any]:
    """What the work tree looks like now, for the receipt.

    Evidence and not a gate: the export reads the object store, so nothing
    here can change what a release contains. It is recorded so an activation
    can be read after the fact.
    """

    scope = [p for p in paths if p]
    try:
        head = _git(repository, "rev-parse", "HEAD").strip()
        status = _git(repository, "status", "--porcelain", "--", *scope) if scope else _git(
            repository, "status", "--porcelain"
        )
    except DomainError as exc:
        return {"head": "", "dirty": None, "error": exc.code, "observed_at": utc_now()}
    changed = [line[3:] for line in status.splitlines() if line.strip()]
    return {
        "head": head,
        "dirty": bool(changed),
        "changed_paths": changed[:50],
        "observed_at": utc_now(),
    }


def _subtree_ids(repository: Path, commit: str, paths: tuple[str, ...]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for path in paths:
        try:
            ids[path] = _git(repository, "rev-parse", "--verify", "--quiet", f"{commit}:{path}").strip()
        except DomainError as exc:
            raise DomainError(
                "work_relay_source_path_missing",
                f"{path} does not exist at {commit[:12]}.",
                details={"commit": commit, "path": path},
            ) from exc
    return ids


def _expected_blobs(repository: Path, commit: str, paths: tuple[str, ...]) -> dict[str, str]:
    """Repo-relative file path to blob id, straight from the commit's trees."""

    listing = _git(repository, "ls-tree", "-r", "-z", commit, "--", *paths)
    blobs: dict[str, str] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, _tab, relative = entry.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        blobs[relative] = parts[2]
    return blobs


def _blob_id(path: Path) -> str:
    data = path.read_bytes()
    digest = hashlib.sha1()
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def verify_release_files(release_dir: Path, expected: Mapping[str, str]) -> list[str]:
    """Every file the commit holds is present with the commit's content. Returns the mismatches."""

    mismatched: list[str] = []
    for relative, blob in expected.items():
        target = release_dir / relative
        if target.is_symlink() or not target.is_file():
            mismatched.append(relative)
            continue
        if _blob_id(target) != blob:
            mismatched.append(relative)
    return mismatched


def _extract_archive(repository: Path, commit: str, paths: tuple[str, ...], destination: Path) -> None:
    process = subprocess.Popen(
        ["git", "-C", str(repository), "archive", "--format=tar", commit, "--", *paths],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if not (member.isfile() or member.isdir()):
                    # The relay source is files and directories. A link or a
                    # device in the archive is not something to write here.
                    raise DomainError(
                        "work_relay_source_archive_member_refused",
                        f"{member.name} is not a regular file or directory.",
                        details={"member": member.name, "type": int(member.type[0])},
                    )
                normalised = Path(member.name)
                if normalised.is_absolute() or ".." in normalised.parts:
                    raise DomainError(
                        "work_relay_source_archive_member_refused",
                        f"{member.name} escapes the release directory.",
                        details={"member": member.name},
                    )
                archive.extract(member, path=destination, set_attrs=False)
    finally:
        _stderr = process.stderr.read() if process.stderr else b""
        returncode = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    if returncode != 0:
        raise DomainError(
            "work_relay_source_git_failed",
            f"git archive failed for {commit[:12]}.",
            details={"commit": commit, "stderr": _stderr.decode("utf-8", "replace").strip()[:500]},
        )


def read_release(release_dir: Path) -> RelaySourceRelease | None:
    marker_path = release_dir / RELEASE_MARKER
    if not marker_path.is_file():
        return None
    try:
        marker = read_json(marker_path)
    except DomainError:
        return None
    schema = str(marker.get("schema") or "")
    if schema not in {
        RELEASE_SCHEMA,
        SINGLE_REPOSITORY_RELEASE_SCHEMA,
        LEGACY_RELEASE_SCHEMA,
    }:
        return None
    source_paths_raw = marker.get("source_paths")
    if isinstance(source_paths_raw, list):
        source_paths = tuple(str(path) for path in source_paths_raw if str(path))
    else:
        source_paths = tuple(
            path
            for path in (
                str(marker.get("tools_path") or ""),
                str(marker.get("app_path") or ""),
            )
            if path
        )
    entrypoint_path = str(marker.get("entrypoint_path") or "")
    if not entrypoint_path and marker.get("tools_path"):
        entrypoint_path = str(Path(str(marker["tools_path"])) / ENTRYPOINT)
    if schema == RELEASE_SCHEMA:
        raw_components = marker.get("components")
        if not isinstance(raw_components, list):
            return None
        try:
            components = normalise_components(raw_components)
            release_id = validate_release_id(marker.get("release_id"))
        except (TypeError, ValueError):
            return None
        if release_id != release_id_for_components(components):
            return None
    else:
        commit = str(marker.get("commit") or "").strip().lower()
        subtrees_raw = marker.get("subtrees")
        if not commit:
            return None
        subtrees = (
            {str(k): str(v) for k, v in subtrees_raw.items()}
            if isinstance(subtrees_raw, Mapping)
            else {}
        )
        components = (
            SourceComponent(
                name=APP_ECOSYSTEM_COMPONENT,
                commit=commit,
                subtrees=subtrees,
                repository=str(marker.get("repository") or ""),
            ),
        )
        release_id = commit
    return RelaySourceRelease(
        release_id=release_id,
        path=release_dir,
        entrypoint_path=entrypoint_path,
        source_paths=source_paths,
        components=components,
        schema=schema,
        tools_path=str(marker.get("tools_path") or ""),
        app_path=str(marker.get("app_path") or ""),
        exported_at=str(marker.get("exported_at") or ""),
    )


def export_release(
    *,
    repository: Path,
    ref: str,
    root: Path,
    tools_path: str = "",
    app_path: str = "",
    entrypoint_path: str = "",
    source_paths: tuple[str, ...] = (),
    between: Callable[[], None] | None = None,
) -> RelaySourceRelease:
    """Export ``ref`` as a release under ``root`` and verify it against the commit.

    ``between`` runs after the ref is pinned to a commit and before the
    export. It exists for the regression test that edits the work tree in
    that gap. Nothing in this module reads the work
    tree after the pin.
    """

    repository = Path(repository).resolve()
    if source_paths:
        paths = tuple(str(path).strip("/") for path in source_paths if str(path).strip("/"))
    else:
        paths = tuple(
            path for path in (tools_path.strip("/"), app_path.strip("/")) if path
        )
    selected_entrypoint = entrypoint_path.strip("/") or str(
        Path(tools_path.strip("/")) / ENTRYPOINT
    )
    if not paths or not selected_entrypoint:
        raise DomainError(
            "work_relay_source_layout_invalid",
            "A client source release needs an entry point and at least one source path.",
        )
    commit = resolve_commit(repository, ref)
    subtrees = _subtree_ids(repository, commit, paths)
    expected = _expected_blobs(repository, commit, paths)
    if between is not None:
        between()

    releases = Path(root) / RELEASES_DIR
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = releases / commit
    existing = read_release(final)
    if existing is not None and not verify_release_files(final, expected):
        return existing
    if final.exists():
        # A release directory whose files no longer match the commit is not a
        # release of that commit, whatever its marker says.
        shutil.rmtree(final)

    stage = releases / f".{commit}.tmp-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    try:
        _extract_archive(repository, commit, paths, stage)
        mismatched = verify_release_files(stage, expected)
        if mismatched:
            raise DomainError(
                "work_relay_source_export_mismatch",
                f"{len(mismatched)} exported file(s) do not match {commit[:12]}.",
                details={"commit": commit, "paths": mismatched[:20]},
            )
        release = RelaySourceRelease(
            release_id=commit,
            path=final,
            entrypoint_path=selected_entrypoint,
            source_paths=paths,
            components=(
                SourceComponent(
                    name=APP_ECOSYSTEM_COMPONENT,
                    commit=commit,
                    subtrees=subtrees,
                    repository=str(repository),
                ),
            ),
            schema=SINGLE_REPOSITORY_RELEASE_SCHEMA,
            tools_path=paths[0],
            app_path=paths[1] if len(paths) > 1 else "",
            exported_at=utc_now(),
        )
        if not (stage / selected_entrypoint).is_file():
            raise DomainError(
                "work_relay_source_entrypoint_missing",
                f"{selected_entrypoint} is not in {commit[:12]}.",
                details={"commit": commit, "entrypoint_path": selected_entrypoint},
            )
        atomic_write_json(stage / RELEASE_MARKER, release.marker())
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return release


def current_release(root: Path) -> RelaySourceRelease | None:
    link = Path(root) / CURRENT_LINK
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except OSError:
        return None
    return read_release(target)


def _selection_subtrees(
    value: Any,
    *,
    path: Path | None = None,
    required_paths: tuple[str, ...] = APP_ECOSYSTEM_SOURCE_PATHS,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise DomainError(
            "work_client_source_selection_invalid",
            "A code Project Board source selection requires its package tree ids.",
            details={"path": str(path)} if path is not None else {},
        )
    subtrees = {str(key): str(tree).lower() for key, tree in value.items()}
    required = set(required_paths)
    invalid_ids = [
        key
        for key, tree in subtrees.items()
        if len(tree) not in {40, 64}
        or any(char not in "0123456789abcdef" for char in tree)
    ]
    if set(subtrees) != required or invalid_ids:
        raise DomainError(
            "work_client_source_selection_invalid",
            "A code Project Board source selection must name every package "
            "tree owned by its repository commit.",
            details={
                "path": str(path) if path is not None else "",
                "required_paths": list(required_paths),
                "actual_paths": sorted(subtrees),
                "invalid_tree_ids": sorted(invalid_ids),
            },
        )
    return subtrees


def _selection_components(
    value: Any, *, path: Path | None = None
) -> tuple[SourceComponent, ...]:
    if not isinstance(value, list):
        raise DomainError(
            "work_client_source_selection_invalid",
            "A code Project Board source selection requires its repository components.",
            details={"path": str(path)} if path is not None else {},
        )
    try:
        return normalise_components(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "work_client_source_selection_invalid",
            f"The Project Board client source components are invalid: {exc}.",
            details={"path": str(path)} if path is not None else {},
        ) from exc


def _validated_composite_selection(
    marker: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    components = _selection_components(marker.get("components"), path=path)
    try:
        release_id = validate_release_id(marker.get("release_id"))
    except ValueError as exc:
        raise DomainError(
            "work_client_source_selection_invalid",
            f"The Project Board client source release id is invalid: {exc}.",
            details={"path": str(path)} if path is not None else {},
        ) from exc
    calculated = release_id_for_components(components)
    if release_id != calculated:
        raise DomainError(
            "work_client_source_selection_invalid",
            "The Project Board client source release id does not match its components.",
            details={
                "path": str(path) if path is not None else "",
                "recorded_release_id": release_id,
                "calculated_release_id": calculated,
            },
        )
    app_ecosystem = component_named(components, APP_ECOSYSTEM_COMPONENT)
    compatibility_commit = str(marker.get("commit") or "").strip().lower()
    if compatibility_commit and compatibility_commit != app_ecosystem.commit:
        raise DomainError(
            "work_client_source_selection_invalid",
            "The compatibility commit differs from the App Ecosystem component.",
            details={"path": str(path) if path is not None else ""},
        )
    compatibility_subtrees = marker.get("subtrees")
    if compatibility_subtrees is not None:
        validated_subtrees = _selection_subtrees(
            compatibility_subtrees,
            path=path,
            required_paths=APP_ECOSYSTEM_SOURCE_PATHS,
        )
        if validated_subtrees != app_ecosystem.subtrees:
            raise DomainError(
                "work_client_source_selection_invalid",
                "The compatibility package trees differ from the App Ecosystem component.",
                details={"path": str(path) if path is not None else ""},
            )
    marker["release_id"] = release_id
    marker["components"] = component_records(
        components, include_repository=False
    )
    marker["commit"] = app_ecosystem.commit
    marker["subtrees"] = dict(app_ecosystem.subtrees)
    return marker


def read_selection(root: Path) -> dict[str, Any]:
    """Read the explicit source selector.

    An empty mapping means no source has been selected yet. The old relay-only
    ``current`` link is not a client selector; only ``selection.json`` can move
    both the command and relay.
    """

    root = Path(root)
    marker = read_json(root / SELECTION_FILE, required=False)
    if marker:
        schema = str(marker.get("schema") or "")
        if schema not in {SELECTION_SCHEMA, LEGACY_SELECTION_SCHEMA}:
            raise DomainError(
                "work_client_source_selection_invalid",
                "The Project Board client source selection has an unsupported schema.",
                details={"path": str(root / SELECTION_FILE)},
            )
        mode = str(marker.get("mode") or "")
        if mode == "released":
            marker["version"] = canonical_release_version(
                marker.get("version"), path=root / SELECTION_FILE
            )
        elif mode == "snapshot" and schema == SELECTION_SCHEMA:
            marker = _validated_composite_selection(
                dict(marker), path=root / SELECTION_FILE
            )
        elif mode == "snapshot":
            commit = str(marker.get("commit") or "").strip().lower()
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                raise DomainError(
                    "work_client_source_selection_invalid",
                    "The code Project Board source selection has no full commit.",
                    details={"path": str(root / SELECTION_FILE)},
                )
            marker["subtrees"] = _selection_subtrees(
                marker.get("subtrees"),
                path=root / SELECTION_FILE,
                required_paths=APP_ECOSYSTEM_SOURCE_PATHS,
            )
        else:
            raise DomainError(
                "work_client_source_selection_invalid",
                "The Project Board client source selection mode is invalid.",
                details={"path": str(root / SELECTION_FILE), "mode": mode},
            )
        return dict(marker)

    return {}


def write_selection(root: Path, selection: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically record the one source both client entry points must use."""

    root = Path(root)
    value = dict(selection)
    # Validate the exact shape before it becomes authoritative.
    if value.get("mode") == "released":
        value["schema"] = SELECTION_SCHEMA
        value["version"] = canonical_release_version(value.get("version"))
    elif value.get("mode") == "snapshot":
        if value.get("components") is not None or value.get("release_id") is not None:
            value["schema"] = SELECTION_SCHEMA
            value = _validated_composite_selection(value)
        else:
            value["schema"] = LEGACY_SELECTION_SCHEMA
            commit = str(value.get("commit") or "").strip().lower()
            if len(commit) != 40 or any(
                char not in "0123456789abcdef" for char in commit
            ):
                raise DomainError(
                    "work_client_source_selection_invalid",
                    "A legacy code Project Board source selection requires a full commit.",
                )
            value["commit"] = commit
            value["subtrees"] = _selection_subtrees(
                value.get("subtrees"),
                required_paths=APP_ECOSYSTEM_SOURCE_PATHS,
            )
    else:
        raise DomainError(
            "work_client_source_selection_invalid",
            "A Project Board source selection must be released or snapshot.",
            details={"mode": str(value.get("mode") or "")},
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(root / SELECTION_FILE, value)
    return value


def clear_selection(root: Path) -> None:
    """Remove an explicit selector while restoring a pre-selection state."""

    (Path(root) / SELECTION_FILE).unlink(missing_ok=True)


def canonical_release_version(value: Any, *, path: Path | None = None) -> str:
    clean = str(value or "").strip()
    try:
        return str(Version(clean))
    except InvalidVersion as exc:
        raise DomainError(
            "work_client_source_selection_invalid",
            "A released Project Board source selection requires a valid package version.",
            details={
                "version": clean,
                "path": str(path) if path is not None else "",
            },
        ) from exc


def released_selection(version: str, *, selected_at: str | None = None) -> dict[str, Any]:
    return {
        "schema": SELECTION_SCHEMA,
        "mode": "released",
        "version": canonical_release_version(version),
        "selected_at": selected_at or utc_now(),
    }


def snapshot_selection(
    release: RelaySourceRelease,
    *,
    selected_at: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": SELECTION_SCHEMA,
        "mode": "snapshot",
        "release_id": release.release_id,
        "components": component_records(
            release.components, include_repository=False
        ),
        "commit": release.commit,
        "subtrees": _selection_subtrees(release.subtrees),
        "release_path": str(release.path),
        "selected_at": selected_at or utc_now(),
    }
    return value


def selected_release(root: Path, selection: Mapping[str, Any] | None = None) -> RelaySourceRelease:
    """Resolve and verify the release named by a snapshot selection."""

    selected = dict(selection or read_selection(root))
    if selected.get("mode") != "snapshot":
        raise DomainError(
            "work_client_source_not_snapshot",
            "The selected Project Board client source is not a code snapshot.",
            details={"mode": str(selected.get("mode") or "")},
        )
    schema = str(selected.get("schema") or LEGACY_SELECTION_SCHEMA)
    legacy = schema == LEGACY_SELECTION_SCHEMA
    identity = str(
        selected.get("commit") if legacy else selected.get("release_id") or ""
    )
    release = read_release(Path(root) / RELEASES_DIR / identity)
    if release is None or release.release_id != identity:
        raise DomainError(
            "work_client_source_release_missing",
            f"The selected Project Board code release {identity[:12]} is missing.",
            details={"release_id": identity, "root": str(root)},
        )
    required_paths = (
        APP_ECOSYSTEM_SOURCE_PATHS if legacy else CLIENT_SOURCE_PATHS
    )
    if (
        release.entrypoint_path != PROJECT_BOARD_CODE_ENTRYPOINT
        or release.source_paths != required_paths
    ):
        raise DomainError(
            "work_client_source_release_mismatch",
            f"The selected Project Board code release {identity[:12]} is not "
            "a complete client source.",
            details={
                "release_id": identity,
                "entrypoint_path": release.entrypoint_path,
                "source_paths": list(release.source_paths),
                "required_entrypoint_path": PROJECT_BOARD_CODE_ENTRYPOINT,
                "required_source_paths": list(required_paths),
            },
        )
    if not legacy:
        expected_components = component_records(
            _selection_components(selected.get("components")),
            include_repository=False,
        )
        release_components = component_records(
            release.components, include_repository=False
        )
        if release.schema != RELEASE_SCHEMA or expected_components != release_components:
            raise DomainError(
                "work_client_source_release_mismatch",
                f"The selected Project Board code release {identity[:12]} "
                "does not match its repository components.",
                details={
                    "release_id": identity,
                    "selected_components": expected_components,
                    "release_components": release_components,
                },
            )
    expected_subtrees = dict(selected.get("subtrees") or {})
    if expected_subtrees and expected_subtrees != release.subtrees:
        raise DomainError(
            "work_client_source_release_mismatch",
            f"The selected Project Board code release {identity[:12]} does "
            "not match its recorded package trees.",
            details={
                "release_id": identity,
                "selected_subtrees": expected_subtrees,
                "release_subtrees": dict(release.subtrees),
            },
        )
    return release


def activate_release(root: Path, release: RelaySourceRelease) -> str:
    """Point ``current`` at the release atomically. Returns the previous release id."""

    root = Path(root)
    previous = current_release(root)
    temporary = root / f".{CURRENT_LINK}.tmp-{os.getpid()}"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(os.path.join(RELEASES_DIR, release.release_id), temporary)
    os.replace(temporary, root / CURRENT_LINK)
    return previous.release_id if previous else ""


def prune_releases(root: Path, keep: tuple[str, ...]) -> list[str]:
    """Remove release directories not in ``keep`` and any abandoned stage. Returns what went."""

    releases = Path(root) / RELEASES_DIR
    if not releases.is_dir():
        return []
    removed: list[str] = []
    for entry in sorted(releases.iterdir()):
        if entry.name in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def prepare_release(
    *,
    repository: Path,
    ref: str,
    root: Path,
    tools_path: str = "",
    app_path: str = "",
    entrypoint_path: str = "",
    source_paths: tuple[str, ...] = (),
    expect: str | None = None,
    between: Callable[[], None] | None = None,
) -> tuple[RelaySourceRelease, dict[str, Any]]:
    """Pin the ref, compare with the approved commit, record the tree's state, export and verify.

    ``expect`` is compared before anything is written: a ref that resolves
    to a commit other than the approved one refuses the whole activation
    so the check lives in the command and not in
    the reader. The swap, the restart and the prune are the caller's, in
    that order, because success is known only once the new process has said
    which commit it loaded.
    """

    repository = Path(repository).resolve()
    commit = resolve_commit(repository, ref)
    approved = str(expect or "").strip().lower()
    if approved and not commit.startswith(approved):
        raise DomainError(
            "work_relay_source_commit_unexpected",
            f"{ref} resolves to {commit[:12]}, not the approved {approved[:12]}. Nothing was changed.",
            details={"ref": ref, "resolved": commit, "expected": approved},
        )
    paths = source_paths or tuple(
        path for path in (tools_path, app_path) if str(path).strip()
    )
    evidence = checkout_evidence(repository, *paths)
    release = export_release(
        repository=repository,
        ref=commit,
        root=root,
        tools_path=tools_path,
        app_path=app_path,
        entrypoint_path=entrypoint_path,
        source_paths=source_paths,
        between=between,
    )
    return release, evidence


@contextmanager
def activation_lock(root: Path) -> Iterator[None]:
    """One activation at a time per host: pin, export, swap, verify and prune under one flock."""

    with exclusive_lock(Path(root) / ".activation.lock"):
        yield


STARTUP_SCHEMA = "problem-board.relay-startup.v1"


def write_startup_record(path: Path, *, pid: int, source: Mapping[str, Any], **extra: Any) -> None:
    """What this relay process is: pid, the source it loaded, and what else the start knew.

    An activation is reported as done only when this record, written by the
    new process itself, names the commit that was approved. Never raises:
    a relay that cannot write its record still runs, and the activation
    that cannot read one reports that it could not.
    """

    try:
        atomic_write_json(
            Path(path),
            {
                "schema": STARTUP_SCHEMA,
                "pid": int(pid),
                "started_at": utc_now(),
                "source": dict(source),
                **extra,
            },
        )
    except OSError:
        return


def read_startup_record(path: Path) -> dict[str, Any]:
    try:
        return read_json(Path(path), required=False)
    except DomainError:
        return {}


def describe_source(
    script: Path, *, scope_paths: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Where a running relay's code came from, for its start line and the status. Never raises.

    ``snapshot``: a release marker sits above the entrypoint, and the commit
    is the fact. ``checkout``: the entrypoint lives in a work tree, and the
    head and dirty flag are evidence about that tree at this instant.
    """

    try:
        resolved = Path(script).resolve()
    except OSError:
        return {"mode": "unknown", "script": str(script)}
    probe = resolved.parent
    for _ in range(MARKER_SEARCH_DEPTH):
        release = read_release(probe)
        if release is not None:
            source: dict[str, Any] = {
                "mode": "snapshot",
                "release_id": release.release_id,
                "commit": release.commit,
                "subtrees": dict(release.subtrees),
                "release_path": str(release.path),
                "exported_at": release.exported_at,
            }
            if release.schema == RELEASE_SCHEMA:
                source["components"] = component_records(
                    release.components, include_repository=False
                )
            return source
        if probe.parent == probe:
            break
        probe = probe.parent
    try:
        repository = repository_root(resolved.parent)
    except DomainError:
        try:
            distribution = metadata.distribution("project-board")
        except metadata.PackageNotFoundError:
            return {"mode": "unknown", "script": str(resolved)}
        package_root = Path(distribution.locate_file("project_board")).resolve()
        if resolved != package_root and package_root not in resolved.parents:
            return {"mode": "unknown", "script": str(resolved)}
        return {
            "mode": "released",
            "version": distribution.version,
            "package_path": str(resolved),
        }
    try:
        relative_source = str(resolved.relative_to(repository))
    except ValueError:
        relative_source = ""
    evidence = (
        checkout_evidence(repository, *scope_paths)
        if scope_paths
        else (
            checkout_evidence(repository, relative_source)
            if relative_source
            else checkout_evidence(repository)
        )
    )
    return {
        "mode": "checkout",
        "repository": str(repository),
        "head": evidence.get("head", ""),
        "dirty": evidence.get("dirty"),
        "observed_at": evidence.get("observed_at", ""),
    }


def source_line(source: Mapping[str, Any]) -> str:
    """The concise source identity written by a client or relay process."""

    mode = str(source.get("mode") or "unknown")
    if mode == "snapshot":
        commits: dict[str, str] = {}
        raw_components = source.get("components")
        if isinstance(raw_components, list):
            for component in raw_components:
                if isinstance(component, Mapping):
                    commits[str(component.get("name") or "")] = str(
                        component.get("commit") or "unknown"
                    )
        if commits:
            return (
                f"source=snapshot release={source.get('release_id') or 'unknown'} "
                f"app_ecosystem={commits.get(APP_ECOSYSTEM_COMPONENT, 'unknown')} "
                f"kdcube={commits.get(KDCUBE_COMPONENT, 'unknown')}"
            )
        return f"source=snapshot commit={source.get('commit') or 'unknown'}"
    if mode == "checkout":
        return (
            f"source=checkout head={source.get('head') or 'unknown'} "
            f"dirty={json.dumps(source.get('dirty')) if source.get('dirty') is not None else 'unknown'}"
        )
    if mode == "released":
        return f"source=released version={source.get('version') or 'unknown'}"
    return f"source={mode}"

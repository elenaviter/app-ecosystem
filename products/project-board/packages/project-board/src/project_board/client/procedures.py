from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, atomic_write_text, exclusive_lock


PROCEDURE_ID = "problem-board-worker"
PROCEDURE_MARKER = "Managed by Problem Board procedure package."
LEGACY_PROCEDURE_MARKER = "Generated from Problem Board procedures/agent-worker.md."
SOURCE_MANIFEST = "package.json"
INSTALLED_MANIFEST = "package-manifest.json"
SOURCE_SCHEMA = "problem-board.procedure-source.v1"
INSTALLED_SCHEMA = "problem-board.procedure-package.v2"
INSTALLED_SOURCE_ENTRYPOINT = "_source/SKILL.md"
_REVISION_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_RELEASE_PATTERN = r"[0-9a-f]{64}-[0-9a-f]{12}"
_REVISION_RE = re.compile(rf"^{_REVISION_PATTERN}$")
_MARKER_RE = re.compile(
    rf"<!-- {re.escape(PROCEDURE_MARKER)} revision=(?P<revision>{_REVISION_PATTERN}) "
    rf"digest=(?P<digest>[0-9a-f]{{64}}) release=(?P<release>{_RELEASE_PATTERN}) -->"
)


def source_package_path() -> Path:
    return Path(__file__).resolve().parents[1] / "procedures" / PROCEDURE_ID


def source_path() -> Path:
    return source_package_path() / "SKILL.md"


def _sha256(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _relative_file(value: Any, *, field: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            f"{field} must be a package-relative file path.",
            details={"field": field, "value": raw},
        )
    return path.as_posix()


def _read_source_package() -> dict[str, Any]:
    root = source_package_path()
    manifest_path = root / SOURCE_MANIFEST
    if manifest_path.is_symlink():
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The Problem Board worker procedure package manifest must be a regular file.",
            details={"path": str(manifest_path)},
        )
    try:
        definition = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "work_agent_procedure_manifest_unreadable",
            "The Problem Board worker procedure package manifest could not be read.",
            details={"path": str(manifest_path), "error": str(exc)},
        ) from exc
    if not isinstance(definition, Mapping) or definition.get("schema") != SOURCE_SCHEMA:
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The Problem Board worker procedure package manifest has an unsupported schema.",
            details={"path": str(manifest_path)},
        )
    expected_definition_fields = {
        "schema",
        "package_id",
        "revision",
        "entrypoint",
        "references",
    }
    if set(definition) != expected_definition_fields:
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package manifest fields do not match its schema.",
            details={
                "path": str(manifest_path),
                "expected_fields": sorted(expected_definition_fields),
                "actual_fields": sorted(str(field) for field in definition),
            },
        )
    if str(definition.get("package_id") or "") != PROCEDURE_ID:
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package id does not match its installation id.",
            details={"path": str(manifest_path)},
        )
    revision = str(definition.get("revision") or "").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package revision must be a portable token.",
            details={"path": str(manifest_path)},
        )
    entrypoint = _relative_file(definition.get("entrypoint"), field="entrypoint")
    if entrypoint != "SKILL.md":
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package entrypoint must be SKILL.md.",
            details={"path": str(manifest_path), "entrypoint": entrypoint},
        )
    raw_references = definition.get("references")
    if not isinstance(raw_references, list):
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package references must be a list.",
            details={"path": str(manifest_path)},
        )
    references = [
        _relative_file(value, field=f"references[{index}]")
        for index, value in enumerate(raw_references)
    ]
    if any(not value.startswith("references/") for value in references):
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "Every worker procedure reference must live under references/.",
            details={"references": references},
        )
    declared = [entrypoint, *references]
    if len(declared) != len(set(declared)):
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure package declares a file more than once.",
            details={"files": declared},
        )

    package_paths = list(root.rglob("*"))
    symlinks = sorted(
        path.relative_to(root).as_posix()
        for path in package_paths
        if path.is_symlink()
    )
    if symlinks:
        raise DomainError(
            "work_agent_procedure_manifest_invalid",
            "The worker procedure source package must not contain symbolic links.",
            details={"paths": symlinks},
        )
    actual = sorted(
        path.relative_to(root).as_posix()
        for path in package_paths
        if path.is_file() and path != manifest_path
    )
    undeclared = sorted(set(actual) - set(declared))
    if undeclared:
        raise DomainError(
            "work_agent_procedure_manifest_incomplete",
            "The worker procedure package contains files that its manifest does not declare.",
            details={"files": undeclared},
        )

    contents: dict[str, str] = {}
    digests: dict[str, str] = {}
    for relative in declared:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise DomainError(
                "work_agent_procedure_file_missing",
                "A declared worker procedure package file is missing or is not a regular file.",
                details={"path": str(path), "relative_path": relative},
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DomainError(
                "work_agent_procedure_file_unreadable",
                "A declared worker procedure package file could not be read.",
                details={"path": str(path), "relative_path": relative},
            ) from exc
        contents[relative] = text
        digests[relative] = _sha256(text)

    digest_input = {
        "schema": SOURCE_SCHEMA,
        "package_id": PROCEDURE_ID,
        "revision": revision,
        "entrypoint": entrypoint,
        "references": references,
        "files": digests,
    }
    package_digest = _sha256(
        json.dumps(digest_input, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    return {
        **digest_input,
        "source_root": str(root),
        "source_manifest": str(manifest_path),
        "source_digest": package_digest,
        "contents": contents,
    }


def source_package() -> dict[str, Any]:
    package = _read_source_package()
    return {key: value for key, value in package.items() if key != "contents"}


def _target_path(target: str, root: Path) -> Path:
    destinations = {
        "codex": root / ".codex" / "skills" / PROCEDURE_ID / "SKILL.md",
        "claude-code": root / ".claude" / "skills" / PROCEDURE_ID / "SKILL.md",
    }
    clean = str(target or "").strip().lower()
    path = destinations.get(clean)
    if path is None:
        raise DomainError(
            "work_agent_procedure_target_invalid",
            "Agent procedure target must be codex or claude-code.",
        )
    return path


def _target_destinations(
    targets: Sequence[str], root: Path
) -> list[tuple[str, Path]]:
    destinations: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw in targets:
        target = str(raw or "").strip().lower()
        destination = _target_path(target, root)
        if target in seen:
            continue
        seen.add(target)
        destinations.append((target, destination))
    return destinations


def _home_path(home: str | Path | None) -> Path:
    """Return an absolute lexical home path without following symlinks."""

    selected = Path(home).expanduser() if home is not None else Path.home()
    return Path(os.path.abspath(selected))


def _first_symlink_component(base: Path, path: Path) -> Path | None:
    """Find the first symlink from ``base`` through ``path``, inclusively."""

    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise DomainError(
            "work_agent_procedure_path_invalid",
            "The worker procedure path must remain inside the selected home.",
            details={"home": str(base), "path": str(path)},
        ) from exc
    current = base
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        if current.is_symlink():
            return current
    return None


def _procedure_path_symlink(destination: Path, path: Path) -> Path | None:
    # target/.codex|.claude/skills/problem-board-worker/SKILL.md
    home = destination.parents[3]
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise DomainError(
            "work_agent_procedure_path_invalid",
            "The worker procedure path must remain inside the selected home.",
            details={"home": str(home), "path": str(path)},
        ) from exc
    return _first_symlink_component(Path(path.anchor), path)


def _require_safe_install_paths(destination: Path) -> None:
    """Reject topology that could redirect an install outside its chosen home."""

    endpoints = (
        destination,
        destination.parent / ".procedure-install.lock",
        destination.parent / "references" / "releases",
    )
    for endpoint in endpoints:
        symlink = _procedure_path_symlink(destination, endpoint)
        if symlink is not None:
            raise DomainError(
                "work_agent_procedure_unsafe_path",
                "The worker procedure installation path contains a symbolic link.",
                status=409,
                details={
                    "path": str(endpoint),
                    "symbolic_link": str(symlink),
                },
            )


def _managed_identity(text: str) -> dict[str, str] | None:
    match = _MARKER_RE.search(text)
    if not match:
        return None
    return {key: str(value) for key, value in match.groupdict().items()}


def _render_entrypoint(package: Mapping[str, Any], release: str) -> str:
    source = str(package["contents"][package["entrypoint"]])
    if PROCEDURE_MARKER in source:
        raise DomainError(
            "work_agent_procedure_entrypoint_invalid",
            "The source SKILL.md must not contain an installed-package marker.",
        )
    reference_root = f"references/releases/{release}/"
    rendered = source
    for relative in package["references"]:
        source_link = f"]({relative})"
        if source_link not in rendered:
            raise DomainError(
                "work_agent_procedure_entrypoint_invalid",
                "Every declared worker procedure reference must be linked from SKILL.md.",
                details={"reference": relative},
            )
        installed_relative = Path(relative).relative_to("references").as_posix()
        rendered = rendered.replace(
            source_link,
            f"]({reference_root}{installed_relative})",
        )
    marker = (
        f"<!-- {PROCEDURE_MARKER} revision={package['revision']} "
        f"digest={package['source_digest']} release={release} -->"
    )
    if rendered.startswith("---\n"):
        end = rendered.find("\n---\n", 4)
        if end < 0:
            raise DomainError(
                "work_agent_procedure_entrypoint_invalid",
                "The worker procedure SKILL.md front matter is not closed.",
            )
        insertion = end + len("\n---\n")
        return rendered[:insertion] + "\n" + marker + "\n" + rendered[insertion:].lstrip("\n")
    return marker + "\n\n" + rendered


def _validate_staged_release(
    stage: Path,
    manifest: Mapping[str, Any],
    package: Mapping[str, Any],
    rendered: str,
) -> None:
    expected_files = {
        INSTALLED_MANIFEST,
        INSTALLED_SOURCE_ENTRYPOINT,
        *(
            Path(relative).relative_to("references").as_posix()
            for relative in package["references"]
        ),
    }
    staged_paths = [
        path
        for path in stage.rglob("*")
        if path.is_file() or path.is_symlink()
    ]
    actual_files = {path.relative_to(stage).as_posix() for path in staged_paths}
    if actual_files != expected_files or any(path.is_symlink() for path in staged_paths):
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure generation does not match its file manifest.",
            details={
                "expected_files": sorted(expected_files),
                "actual_files": sorted(actual_files),
            },
        )

    manifest_path = stage / INSTALLED_MANIFEST
    try:
        staged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure manifest is unreadable.",
            details={"path": str(manifest_path), "error": str(exc)},
        ) from exc
    if staged_manifest != dict(manifest):
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure manifest does not match the intended package.",
            details={"path": str(manifest_path)},
        )
    if _sha256(rendered) != str(manifest["entrypoint_installed_sha256"]):
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure entrypoint digest is inconsistent.",
        )

    source_entrypoint = stage / INSTALLED_SOURCE_ENTRYPOINT
    try:
        source_text = source_entrypoint.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure source entrypoint is unreadable.",
            details={"path": str(source_entrypoint), "error": str(exc)},
        ) from exc
    if _sha256(source_text) != package["files"][package["entrypoint"]]:
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure source entrypoint failed digest verification.",
            details={"path": str(source_entrypoint)},
        )
    if _render_entrypoint(package, str(manifest["release"])) != rendered:
        raise DomainError(
            "work_agent_procedure_stage_invalid",
            "The staged worker procedure entrypoint is not reproducible from its source.",
        )

    for relative in package["references"]:
        installed_relative = Path(relative).relative_to("references")
        actual = _sha256((stage / installed_relative).read_bytes())
        expected = package["files"][relative]
        if actual != expected:
            raise DomainError(
                "work_agent_procedure_stage_invalid",
                "A staged worker procedure reference failed digest verification.",
                details={"file": relative, "expected": expected, "actual": actual},
            )


def _install_release(
    destination: Path,
    package: Mapping[str, Any],
) -> tuple[str, str]:
    releases = destination.parent / "references" / "releases"
    _require_safe_install_paths(destination)
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_safe_install_paths(destination)
    release = f"{package['source_digest']}-{uuid.uuid4().hex[:12]}"
    stage = releases / f".{release}.tmp"
    final = releases / release
    rendered = _render_entrypoint(package, release)
    manifest = {
        "schema": INSTALLED_SCHEMA,
        "package_id": PROCEDURE_ID,
        "source_schema": SOURCE_SCHEMA,
        "source_revision": package["revision"],
        "source_digest": package["source_digest"],
        "source_entrypoint": package["entrypoint"],
        "source_entrypoint_snapshot": INSTALLED_SOURCE_ENTRYPOINT,
        "source_references": package["references"],
        "source_files": package["files"],
        "release": release,
        "entrypoint": "SKILL.md",
        "entrypoint_source_sha256": package["files"][package["entrypoint"]],
        "entrypoint_installed_sha256": _sha256(rendered),
    }
    try:
        stage.mkdir(mode=0o700)
        atomic_write_text(
            stage / INSTALLED_SOURCE_ENTRYPOINT,
            package["contents"][package["entrypoint"]],
        )
        for relative in package["references"]:
            installed_relative = Path(relative).relative_to("references")
            atomic_write_text(stage / installed_relative, package["contents"][relative])
        atomic_write_json(stage / INSTALLED_MANIFEST, manifest)
        _validate_staged_release(stage, manifest, package, rendered)
        _require_safe_install_paths(destination)
        os.replace(stage, final)
        # SKILL.md is the package pointer. It changes only after the complete,
        # immutable reference generation and its manifest are readable.
        _require_safe_install_paths(destination)
        atomic_write_text(destination, rendered)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return release, rendered


def _prune_releases(
    destination: Path, current: str, previous: str | None = None
) -> list[str]:
    """Remove every release generation except the current one and the one it replaced.

    The caller holds the install lock. The current generation is the one
    SKILL.md points at. The previous one stays because a session that loaded
    the previous SKILL.md still follows that generation's links until it
    re-reads. When the caller does not know which generation was replaced (an
    install that found the host already current), the newest other generation
    by modification time is taken as the previous one: installs are serialized
    per target and each creates one generation, so the newest other one is the
    one the current SKILL.md replaced. Older generations are pointed at by no
    SKILL.md on disk; left in place, a reader that finds a reference by
    directory listing instead of through the SKILL.md pointer reads stale
    instructions with nothing marking them stale.
    """

    releases = destination.parent / "references" / "releases"
    if not releases.is_dir():
        return []
    generations = [
        path
        for path in releases.iterdir()
        if path.is_dir() and not path.is_symlink()
    ]
    if not previous:
        others = sorted(
            (path for path in generations if path.name != current),
            key=lambda path: path.stat().st_mtime,
        )
        previous = others[-1].name if others else ""
    kept = {current, previous}
    pruned: list[str] = []
    for path in sorted(generations, key=lambda path: path.name):
        if path.name in kept:
            continue
        shutil.rmtree(path)
        pruned.append(path.name)
    return pruned


def _verify_destination(
    target: str,
    destination: Path,
    package: Mapping[str, Any],
) -> dict[str, Any]:
    base = {
        "target": target,
        "path": str(destination),
        "source_revision": package["revision"],
        "source_digest": package["source_digest"],
    }
    destination_symlink = _procedure_path_symlink(destination, destination)
    if destination_symlink is not None:
        return {
            **base,
            "state": "conflict",
            "installed_format": "symbolic-link",
            "errors": [
                "The worker procedure path contains a symbolic link: "
                f"{destination_symlink}"
            ],
        }
    if not destination.exists():
        return {**base, "state": "missing", "errors": ["SKILL.md is not installed."]}
    try:
        text = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            **base,
            "state": "conflict",
            "installed_format": "unreadable",
            "errors": [f"SKILL.md ownership could not be verified: {exc}"],
        }
    identity = _managed_identity(text)
    if identity is None:
        if PROCEDURE_MARKER in text:
            return {
                **base,
                "state": "corrupt",
                "installed_format": "versioned-package",
                "errors": ["The installed package marker is malformed."],
            }
        if LEGACY_PROCEDURE_MARKER in text:
            return {
                **base,
                "state": "stale",
                "installed_format": "legacy-single-file",
                "errors": ["The installed skill predates the versioned procedure package."],
            }
        return {
            **base,
            "state": "conflict",
            "installed_format": "foreign",
            "errors": ["SKILL.md is not owned by Problem Board."],
        }

    errors: list[str] = []
    files_verified = 0
    release = identity["release"]
    release_root = destination.parent / "references" / "releases" / release
    release_symlink = _procedure_path_symlink(destination, release_root)
    if release_symlink is not None:
        return {
            **base,
            "state": "corrupt",
            "installed_format": "versioned-package",
            "installed_revision": identity["revision"],
            "installed_digest": identity["digest"],
            "release": release,
            "files_verified": 0,
            "errors": [
                "The installed package release path contains a symbolic link: "
                f"{release_symlink}"
            ],
        }
    manifest_path = release_root / INSTALLED_MANIFEST
    manifest: dict[str, Any] = {}
    if manifest_path.is_symlink():
        errors.append("The installed package manifest is a symbolic link.")
    else:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                manifest = dict(loaded)
            else:
                errors.append("The installed package manifest is not an object.")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"The installed package manifest is unreadable: {exc}")

    if manifest:
        expected_manifest_fields = {
            "schema",
            "package_id",
            "source_schema",
            "source_revision",
            "source_digest",
            "source_entrypoint",
            "source_entrypoint_snapshot",
            "source_references",
            "source_files",
            "release",
            "entrypoint",
            "entrypoint_source_sha256",
            "entrypoint_installed_sha256",
        }
        if set(manifest) != expected_manifest_fields:
            errors.append(
                "The installed package manifest fields do not match its schema."
            )
        expected_fields = {
            "schema": INSTALLED_SCHEMA,
            "package_id": PROCEDURE_ID,
            "source_schema": SOURCE_SCHEMA,
            "source_revision": identity["revision"],
            "source_digest": identity["digest"],
            "release": release,
            "entrypoint": "SKILL.md",
        }
        for field, expected in expected_fields.items():
            if str(manifest.get(field) or "") != expected:
                errors.append(f"Manifest {field} does not match the installed entrypoint.")
        root_matches_manifest = _sha256(text) == str(
            manifest.get("entrypoint_installed_sha256") or ""
        )
        if not root_matches_manifest:
            errors.append("The installed SKILL.md digest does not match its manifest.")
        source_entrypoint = str(manifest.get("source_entrypoint") or "")
        source_entrypoint_snapshot = str(
            manifest.get("source_entrypoint_snapshot") or ""
        )
        source_references = manifest.get("source_references")
        source_files = manifest.get("source_files")
        if source_entrypoint != "SKILL.md":
            errors.append("The installed source entrypoint path is invalid.")
        if not isinstance(source_references, list) or not isinstance(source_files, Mapping):
            errors.append("The installed package source-file manifest is missing.")
        else:
            digest_input = {
                "schema": SOURCE_SCHEMA,
                "package_id": PROCEDURE_ID,
                "revision": identity["revision"],
                "entrypoint": source_entrypoint,
                "references": source_references,
                "files": dict(source_files),
            }
            calculated_digest = _sha256(
                json.dumps(
                    digest_input,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if calculated_digest != identity["digest"]:
                errors.append(
                    "The installed package manifest does not reproduce its package digest."
                )
            if str(source_files.get(source_entrypoint) or "") != str(
                manifest.get("entrypoint_source_sha256") or ""
            ):
                errors.append("The source entrypoint digest does not match its file manifest.")

            source_text = ""
            source_snapshot_ok = False
            if source_entrypoint_snapshot != INSTALLED_SOURCE_ENTRYPOINT:
                errors.append("The installed source entrypoint snapshot path is invalid.")
            else:
                snapshot_path = release_root / INSTALLED_SOURCE_ENTRYPOINT
                snapshot_symlink = _first_symlink_component(
                    release_root, snapshot_path
                )
                if snapshot_symlink is not None:
                    errors.append(
                        "The installed source entrypoint snapshot path contains a "
                        f"symbolic link: {snapshot_symlink}"
                    )
                else:
                    try:
                        source_text = snapshot_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError) as exc:
                        errors.append(
                            "The installed source entrypoint snapshot is unreadable: "
                            f"{exc}"
                        )
                    else:
                        source_snapshot_ok = _sha256(source_text) == str(
                            source_files.get(source_entrypoint) or ""
                        )
                        if not source_snapshot_ok:
                            errors.append(
                                "The installed source entrypoint snapshot digest does "
                                "not match."
                            )

            clean_references: list[tuple[str, Path]] = []
            for relative in source_references:
                if not isinstance(relative, str):
                    errors.append(f"Installed reference path is invalid: {relative}")
                    continue
                try:
                    clean = _relative_file(relative, field="installed reference")
                    installed_relative = Path(clean).relative_to("references")
                except (DomainError, ValueError):
                    errors.append(f"Installed reference path is invalid: {relative}")
                    continue
                if clean != relative:
                    errors.append(f"Installed reference path is not canonical: {relative}")
                    continue
                clean_references.append((clean, installed_relative))
                path = release_root / installed_relative
                reference_symlink = _first_symlink_component(release_root, path)
                if reference_symlink is not None:
                    errors.append(
                        "Installed reference path contains a symbolic link: "
                        f"{relative}: {reference_symlink}"
                    )
                    continue
                try:
                    actual = _sha256(path.read_bytes())
                except OSError as exc:
                    errors.append(f"Installed reference is unreadable: {relative}: {exc}")
                    continue
                if actual != str(source_files.get(relative) or ""):
                    errors.append(f"Installed reference digest does not match: {relative}")
                else:
                    files_verified += 1

            clean_reference_names = [clean for clean, _ in clean_references]
            if len(clean_reference_names) != len(set(clean_reference_names)):
                errors.append("The installed package declares a reference more than once.")
            expected_source_files = {source_entrypoint, *clean_reference_names}
            if set(source_files) != expected_source_files:
                errors.append(
                    "The installed source-file digests do not match the declared files."
                )

            expected_release_files = {
                INSTALLED_MANIFEST,
                INSTALLED_SOURCE_ENTRYPOINT,
                *(path.as_posix() for _, path in clean_references),
            }
            release_paths = (
                list(release_root.rglob("*")) if release_root.is_dir() else []
            )
            actual_release_files = {
                path.relative_to(release_root).as_posix()
                for path in release_paths
                if path.is_file() or path.is_symlink()
            }
            if actual_release_files != expected_release_files:
                errors.append(
                    "The installed release files do not match its package manifest."
                )
            if any(path.is_symlink() for path in release_paths):
                errors.append("The installed release contains a symbolic link.")

            root_reproducible = False
            if (
                source_snapshot_ok
                and source_entrypoint == "SKILL.md"
                and len(clean_references) == len(source_references)
            ):
                installed_package = {
                    "contents": {source_entrypoint: source_text},
                    "entrypoint": source_entrypoint,
                    "references": clean_reference_names,
                    "revision": identity["revision"],
                    "source_digest": identity["digest"],
                }
                try:
                    expected_entrypoint = _render_entrypoint(
                        installed_package, release
                    )
                except DomainError as exc:
                    errors.append(
                        "The installed SKILL.md cannot be reproduced from its source "
                        f"snapshot: {exc.message}"
                    )
                else:
                    root_reproducible = expected_entrypoint == text
                    if not root_reproducible:
                        errors.append(
                            "The installed SKILL.md is not the deterministic rendering "
                            "of its source snapshot."
                        )
            if root_matches_manifest and root_reproducible:
                files_verified += 1

    installed_digest = identity["digest"]
    state = (
        "corrupt"
        if errors
        else "current"
        if installed_digest == package["source_digest"]
        else "stale"
    )
    return {
        **base,
        "state": state,
        "installed_format": "versioned-package",
        "installed_revision": identity["revision"],
        "installed_digest": installed_digest,
        "release": release,
        "release_path": str(release_root),
        "files_verified": files_verified,
        "errors": errors,
    }


def verify_agent_procedure(
    targets: Sequence[str], *, home: str | Path | None = None
) -> list[dict[str, Any]]:
    root = _home_path(home)
    package = _read_source_package()
    return [
        _verify_destination(target, destination, package)
        for target, destination in _target_destinations(targets, root)
    ]


def install_agent_procedure(
    targets: Sequence[str], *, home: str | Path | None = None, force: bool = False
) -> list[dict[str, Any]]:
    root = _home_path(home)
    package = _read_source_package()
    destinations = _target_destinations(targets, root)

    def reject_unsafe_paths() -> None:
        for _, destination in destinations:
            _require_safe_install_paths(destination)

    def inspect_all() -> list[tuple[str, Path, dict[str, Any]]]:
        return [
            (
                target,
                destination,
                _verify_destination(target, destination, package),
            )
            for target, destination in destinations
        ]

    def reject_conflict(
        requested: Sequence[tuple[str, Path, Mapping[str, Any]]],
    ) -> None:
        if force:
            return
        conflict = next(
            (item for item in requested if item[2]["state"] == "conflict"),
            None,
        )
        if conflict is not None:
            raise DomainError(
                "work_agent_procedure_conflict",
                "The destination already contains a procedure not owned by Problem Board.",
                status=409,
                details={"path": str(conflict[1])},
            )

    reject_unsafe_paths()
    reject_conflict(inspect_all())
    installed: list[dict[str, Any]] = []
    with ExitStack() as locks:
        for _, destination in sorted(destinations, key=lambda item: str(item[1])):
            locks.enter_context(
                exclusive_lock(destination.parent / ".procedure-install.lock")
            )
        reject_unsafe_paths()
        requested = inspect_all()
        reject_conflict(requested)
        for target, destination, before in requested:
            if before["state"] == "current":
                # Already current: nothing to write, and the generations an
                # earlier installer left behind still go.
                pruned = _prune_releases(destination, before["release"])
                installed.append({**before, "pruned_releases": pruned})
                continue
            release, _ = _install_release(destination, package)
            after = _verify_destination(target, destination, package)
            if after["state"] != "current" or after["release"] != release:
                raise DomainError(
                    "work_agent_procedure_install_invalid",
                    "The installed worker procedure package did not pass verification.",
                    details={"path": str(destination), "verification": after},
                )
            pruned = _prune_releases(
                destination, release, str(before.get("release") or "")
            )
            installed.append(
                {
                    **after,
                    "state": "installed",
                    "previous_state": before["state"],
                    "previous_digest": before.get("installed_digest", ""),
                    "previous_release": str(before.get("release") or ""),
                    "pruned_releases": pruned,
                }
            )
    return installed


__all__ = [
    "LEGACY_PROCEDURE_MARKER",
    "PROCEDURE_ID",
    "PROCEDURE_MARKER",
    "install_agent_procedure",
    "source_package",
    "source_package_path",
    "source_path",
    "verify_agent_procedure",
]

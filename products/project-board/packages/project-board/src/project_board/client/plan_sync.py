"""The plan index, written into the project's journal repository as text.

Postgres holds the live index. This writes what it holds into Git, so a machine
with the repository and no database can still read the plan, and so a person can
review the plan's shape in a diff rather than by querying.

Two properties make it safe to run often. It is idempotent: nothing changed
means no commit, so a scheduled run does not fill history with noise. And it
reports what it wrote in items and status changes rather than only that it ran,
because "synced" tells a reader nothing they can act on.

The drift check answers the same question without writing, so anyone can see
whether the committed copy is behind and by how much before deciding to sync.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from ..contract.plan_nodes import parse_plan_node_ref
from ..contract.refs import REFERENCE_STATE_VERSION_RE, parse_ref
from .io import atomic_write_text, content_hash
from .journals import _frontmatter

PLAN_DOCUMENT = "plan/index.md"
PLAN_DATA = "plan/index.json"
PLAN_NODE_ROOT = "plan/nodes"
WORK_REF_CONTINUATION = r"A-Za-z0-9_:-"


def _rows(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in (index.get("items") or []) if isinstance(row, Mapping)]


def plan_export_package(index: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete, integrity-addressed Git import package."""

    rows = sorted(_rows(index), key=lambda row: int(row.get("ordinal") or 0))
    generation_token = str(index.get("generation_token") or "").strip()
    if not generation_token:
        raise DomainError(
            "plan_sync_generation_missing",
            "The PostgreSQL plan index did not identify its generation.",
            status=409,
        )
    package = {
        "schema": str(index.get("schema") or ""),
        "project_ref": str(index.get("project_ref") or ""),
        "plan_revision": int(index.get("plan_revision") or 0),
        "generation_token": generation_token,
        "item_count": len(rows),
        "items": rows,
    }
    package["package_content_hash"] = content_hash(package)
    return package


def render_plan_document(index: Mapping[str, Any]) -> str:
    """The plan as reviewable text, in plan order.

    A table rather than prose because the point is diffability: a status change
    should be one changed line, not a reflowed paragraph. Each row links to the
    complete node document, where canonical dependency and attachment URIs stay
    reviewable without querying Postgres.
    """

    rows = sorted(_rows(index), key=lambda row: int(row.get("ordinal") or 0))
    by_ref = {str(row.get("item_ref") or ""): str(row.get("item_key") or "") for row in rows}
    lines = [
        "# Plan index",
        "",
        f"Project: `{index.get('project_ref') or ''}`",
        f"Plan revision: {int(index.get('plan_revision') or 0)}",
        f"Items: {len(rows)}",
        "",
        "Each node file carries its searchable source, summary, dependencies, "
        "attachment references, and derived-state hashes.",
        "",
        "| # | Key | Status | Assignee | Title | Node | Depends on |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        path = node_document_path(row)
        depends = ", ".join(
            by_ref.get(str(ref), str(ref)) for ref in (row.get("depends_on") or [])
        )
        lines.append(
            "| {ordinal} | {key} | {status} | {assignee} | {title} | "
            "[open]({path}) | {depends} |".format(
                ordinal=int(row.get("ordinal") or 0),
                key=str(row.get("item_key") or ""),
                status=str(row.get("status") or ""),
                assignee=str(row.get("assignee") or "") or "-",
                title=str(row.get("title") or "").replace("|", "\\|"),
                path=Path(path).relative_to("plan").as_posix(),
                depends=depends or "-",
            )
        )
    return "\n".join(lines) + "\n"


def node_document_path(row: Mapping[str, Any]) -> str:
    address = parse_plan_node_ref(str(row.get("item_ref") or ""))
    return f"{PLAN_NODE_ROOT}/{address.bucket}/{address.key}-{address.semantic_name}.md"


def _yaml_scalar(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=True)


def _yaml_list(name: str, values: Sequence[Any]) -> list[str]:
    result = [f"{name}:"]
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return [f"{name}: []"]
    result.extend(f"  - {_yaml_scalar(value)}" for value in cleaned)
    return result


def render_plan_node(row: Mapping[str, Any]) -> str:
    """One complete, diffable node with URI-only attachment references."""

    item_ref = parse_plan_node_ref(str(row.get("item_ref") or "")).ref
    lines = [
        "---",
        "schema: problem-board.plan-node-export.v1",
        f"uri: {_yaml_scalar(item_ref)}",
        f"key: {_yaml_scalar(row.get('item_key'))}",
        f"title: {_yaml_scalar(row.get('title'))}",
        f"status: {_yaml_scalar(row.get('status'))}",
        f"revision: {int(row.get('revision') or 0)}",
        f"assignee: {_yaml_scalar(row.get('assignee'))}",
        f"ordinal: {int(row.get('ordinal') or 0)}",
        f"updated_at: {_yaml_scalar(row.get('updated_at'))}",
        f"source_content_hash: {_yaml_scalar(row.get('source_content_hash'))}",
        f"summary_source_hash: {_yaml_scalar(row.get('summary_source_hash'))}",
        f"search_content_hash: {_yaml_scalar(row.get('search_content_hash'))}",
        *_yaml_list("tags", row.get("tags") or []),
        *_yaml_list("keywords", row.get("keywords") or []),
        *_yaml_list("depends_on", row.get("depends_on") or []),
        *_yaml_list("attachment_refs", row.get("attachment_refs") or []),
        "---",
        "",
        f"# {str(row.get('item_key') or '').strip()} {str(row.get('title') or '').strip()}".rstrip(),
        "",
        "## Summary",
        "",
        str(row.get("summary") or "").strip() or "No summary.",
        "",
        "## Description",
        "",
        str(row.get("description") or "").strip() or "No description.",
        "",
        "## Acceptance",
        "",
    ]
    acceptance = [str(value).strip() for value in row.get("acceptance") or [] if str(value).strip()]
    lines.extend(f"- {value}" for value in acceptance)
    if not acceptance:
        lines.append("No acceptance criteria.")
    lines.append("")
    return "\n".join(lines)


def read_plan_export(
    journal_home: Path,
    *,
    target_project_ref: str = "",
) -> dict[str, Any]:
    """Validate one complete Git export and prepare its atomic PG write.

    The JSON file carries ordering and the complete structured rows. The
    Markdown files are checked byte-for-byte against that structure so a
    partial copy, a missing node, or two representations that disagree can
    never reach the authoritative transaction.
    """

    root = Path(journal_home).expanduser().resolve()
    data_path = root / PLAN_DATA
    try:
        value = json.loads(data_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DomainError(
            "plan_import_package_missing",
            "The plan export has no plan/index.json package.",
            details={"path": str(data_path)},
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "plan_import_package_invalid",
            "The plan/index.json package is not readable JSON.",
            details={"path": str(data_path)},
        ) from exc
    if not isinstance(value, Mapping):
        raise DomainError(
            "plan_import_package_invalid",
            "The plan/index.json package must contain one JSON object.",
            details={"path": str(data_path)},
        )
    package = dict(value)
    if str(package.get("schema") or "") != "problem-board.plan-index.v2":
        raise DomainError(
            "plan_import_schema_invalid",
            "The plan export does not use the supported plan-index schema.",
            details={"schema": str(package.get("schema") or "")},
        )
    supplied_package_hash = str(package.get("package_content_hash") or "").strip()
    package_without_hash = dict(package)
    package_without_hash.pop("package_content_hash", None)
    if not supplied_package_hash or supplied_package_hash != content_hash(
        package_without_hash
    ):
        raise DomainError(
            "plan_import_hash_invalid",
            "The plan export package hash does not match its contents.",
            status=409,
        )

    source_project_ref = str(package.get("project_ref") or "").strip()
    try:
        if parse_ref(source_project_ref).selector != "project":
            raise ValueError(source_project_ref)
    except (DomainError, ValueError) as exc:
        raise DomainError(
            "plan_import_project_ref_invalid",
            "The plan export must name one canonical work:project URI.",
            details={"project_ref": source_project_ref},
        ) from exc

    selected_project_ref = str(target_project_ref or source_project_ref).strip()
    try:
        if parse_ref(selected_project_ref).selector != "project":
            raise ValueError(selected_project_ref)
    except (DomainError, ValueError) as exc:
        raise DomainError(
            "plan_import_project_ref_invalid",
            "The import target must be one canonical work:project URI.",
            details={"project_ref": selected_project_ref},
        ) from exc

    raw_rows = package.get("items")
    if not isinstance(raw_rows, list):
        raise DomainError(
            "plan_import_items_invalid",
            "The plan export must carry an items list.",
        )
    rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)]
    try:
        declared = int(package["item_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError(
            "plan_import_item_count_invalid",
            "The plan export must declare its item count.",
        ) from exc
    if len(rows) != len(raw_rows) or declared != len(rows):
        raise DomainError(
            "plan_import_package_incomplete",
            "The plan export item count does not match its structured rows.",
            details={"declared": declared, "carried": len(rows)},
        )

    expected_nodes: dict[str, str] = {}
    item_refs: set[str] = set()
    item_ids: set[str] = set()
    for position, row in enumerate(rows):
        item_ref = str(row.get("item_ref") or "").strip()
        try:
            parse_plan_node_ref(item_ref)
        except DomainError as exc:
            raise DomainError(
                "plan_import_node_ref_invalid",
                "A plan export node has an invalid canonical URI.",
                details={"position": position, "item_ref": item_ref},
            ) from exc
        item_id = str(row.get("item_id") or "").strip()
        if not item_id:
            raise DomainError(
                "plan_import_node_id_missing",
                "A plan export node has no item_id.",
                details={"position": position, "item_ref": item_ref},
            )
        if item_ref in item_refs or item_id in item_ids:
            raise DomainError(
                "plan_import_node_identity_duplicate",
                "A plan export repeats a node URI or item_id.",
                details={"position": position, "item_ref": item_ref, "item_id": item_id},
            )
        if int(row.get("ordinal") or 0) != position:
            raise DomainError(
                "plan_import_node_order_invalid",
                "A plan export node ordinal does not match its authored position.",
                details={"position": position, "ordinal": row.get("ordinal")},
            )
        item_refs.add(item_ref)
        item_ids.add(item_id)
        relative = node_document_path(row)
        if relative in expected_nodes:
            raise DomainError(
                "plan_import_node_path_duplicate",
                "Two plan export nodes resolve to the same Markdown path.",
                details={"path": relative},
            )
        expected_nodes[relative] = render_plan_node(row)

    for relative, expected in expected_nodes.items():
        path = root / relative
        try:
            observed = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise DomainError(
                "plan_import_node_missing",
                "The plan export is missing a node document.",
                details={"path": relative},
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise DomainError(
                "plan_import_node_unreadable",
                "A plan export node document cannot be read.",
                details={"path": relative},
            ) from exc
        if observed != expected:
            raise DomainError(
                "plan_import_node_mismatch",
                "A plan node document disagrees with plan/index.json.",
                details={"path": relative},
            )

    node_root = root / PLAN_NODE_ROOT
    observed_nodes = {
        path.relative_to(root).as_posix()
        for path in node_root.rglob("*.md")
    } if node_root.is_dir() else set()
    if observed_nodes != set(expected_nodes):
        raise DomainError(
            "plan_import_node_set_mismatch",
            "The plan export node directory is not the same complete set as plan/index.json.",
            details={
                "missing": sorted(set(expected_nodes) - observed_nodes),
                "unexpected": sorted(observed_nodes - set(expected_nodes)),
            },
        )

    document_path = root / PLAN_DOCUMENT
    try:
        document = document_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DomainError(
            "plan_import_document_missing",
            "The plan export has no plan/index.md document.",
            details={"path": str(document_path)},
        ) from exc
    if document != render_plan_document(package):
        raise DomainError(
            "plan_import_document_mismatch",
            "The plan/index.md document disagrees with plan/index.json.",
            details={"path": str(document_path)},
        )

    publication = dict(package_without_hash)
    publication.update(
        project_ref=selected_project_ref,
        item_count=len(rows),
        items=rows,
    )
    publication["package_content_hash"] = content_hash(publication)
    return {
        "source_project_ref": source_project_ref,
        "target_project_ref": selected_project_ref,
        "publication": publication,
    }


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise DomainError(
            "plan_sync_git_failed",
            f"git {' '.join(args)} failed in the journal repository.",
            details={"stderr": result.stderr.strip()[:400]},
        )
    return result.stdout


def _repository_root(journal_home: Path) -> Path:
    try:
        root = Path(_git(journal_home, "rev-parse", "--show-toplevel").strip()).resolve()
    except DomainError as exc:
        raise DomainError(
            "plan_sync_not_a_repository",
            "The journal home is not inside a Git repository, so the plan cannot be committed.",
            details={"journal_home": str(journal_home)},
        ) from exc
    try:
        journal_home.relative_to(root)
    except ValueError as exc:
        raise DomainError(
            "plan_sync_not_a_repository",
            "The journal home does not belong to its resolved Git repository.",
            details={"journal_home": str(journal_home), "repository": str(root)},
        ) from exc
    return root


def _repo_relative(repository: Path, path: Path) -> str:
    return path.resolve().relative_to(repository).as_posix()


def _tracked_journal_files(repository: Path, journal_home: Path) -> tuple[str, ...]:
    journal_root = journal_home / "journal"
    if not journal_root.is_dir():
        return ()
    journal_pathspec = _repo_relative(repository, journal_root)
    return tuple(
        relative
        for relative in _git(
            repository,
            "ls-files",
            "-z",
            "--",
            journal_pathspec,
        ).split("\0")
        if relative
    )


@dataclass(frozen=True)
class PlanSyncResult:
    changed: bool
    committed: str
    item_count: int
    plan_revision: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    status_changes: tuple[str, ...]
    content_changes: tuple[str, ...]
    written_files: tuple[str, ...]
    rewritten_reference_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "commit": self.committed,
            "item_count": self.item_count,
            "plan_revision": self.plan_revision,
            "added": list(self.added),
            "removed": list(self.removed),
            "status_changes": list(self.status_changes),
            "content_changes": list(self.content_changes),
            "written_files": list(self.written_files),
            "rewritten_reference_files": list(self.rewritten_reference_files),
        }


def _previous(repository: Path) -> dict[str, Any]:
    path = repository / PLAN_DATA
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt copy is not a reason to refuse to write a good one.
        return {}


def _differences(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[list[str], list[str], list[str], list[str]]:
    """What a reader needs to know, not the whole diff.

    Reported by item key rather than count, because "3 items changed" sends the
    reader to the diff anyway while "W12 ready to working" often ends the
    question.
    """

    before = {str(row.get("item_key") or ""): row for row in _rows(previous)}
    after = {str(row.get("item_key") or ""): row for row in _rows(current)}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changes = []
    content_changes = []
    for key in sorted(set(before) & set(after)):
        was = str(before[key].get("status") or "")
        now = str(after[key].get("status") or "")
        if was != now:
            changes.append(f"{key} {was} to {now}")
        before_hash = str(
            before[key].get("source_content_hash")
            or before[key].get("search_content_hash")
            or ""
        )
        after_hash = str(
            after[key].get("source_content_hash")
            or after[key].get("search_content_hash")
            or ""
        )
        if before_hash != after_hash:
            content_changes.append(key)
    return added, removed, changes, content_changes


def plan_drift(repository: Path, index: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the committed copy is behind, without writing anything."""

    journal_home = Path(repository).expanduser().resolve()
    repository = _repository_root(journal_home)
    previous = _previous(journal_home)
    current_package = plan_export_package(index)
    added, removed, changes, content_changes = _differences(previous, index)
    current_refs = {
        str(row.get("item_ref") or "")
        for row in _rows(index)
        if str(row.get("item_ref") or "")
    }
    unresolved: dict[str, list[str]] = {}
    for relative in _tracked_journal_files(repository, journal_home):
        path = repository / relative
        try:
            metadata, _ = _frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        work_ref = str(metadata.get("work_ref") or "").strip()
        if (
            work_ref.startswith(("work:plan:node:", "work:item:"))
            and work_ref not in current_refs
        ):
            unresolved.setdefault(work_ref, []).append(relative)
    unresolved_refs = sorted(unresolved)
    reference_drift_files = sorted(
        {relative for paths in unresolved.values() for relative in paths}
    )
    committed_revision = int(previous.get("plan_revision") or 0)
    live_revision = int(index.get("plan_revision") or 0)
    committed_package_hash = str(previous.get("package_content_hash") or "")
    live_package_hash = str(current_package["package_content_hash"])
    return {
        "in_sync": not (
            added
            or removed
            or changes
            or content_changes
            or unresolved_refs
        )
        and committed_revision == live_revision
        and committed_package_hash == live_package_hash,
        "committed_plan_revision": committed_revision,
        "live_plan_revision": live_revision,
        "revisions_behind": max(0, live_revision - committed_revision),
        "added": added,
        "removed": removed,
        "status_changes": changes,
        "content_changes": content_changes,
        "unresolved_node_references": unresolved_refs,
        "reference_drift_files": reference_drift_files,
        "committed_copy_present": bool(previous),
        "committed_package_content_hash": committed_package_hash,
        "live_package_content_hash": live_package_hash,
    }


def _rewrite_tracked_references(
    repository: Path,
    journal_home: Path,
    mapping: Mapping[str, str],
    *,
    reviewed_files: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[str, ...]:
    preview = tracked_reference_rewrite_preview(
        repository,
        journal_home,
        mapping,
    )
    if reviewed_files is None:
        selected = {str(row["path"]): row for row in preview}
    else:
        expected: dict[str, Mapping[str, Any]] = {}
        for raw in reviewed_files:
            if not isinstance(raw, Mapping):
                raise DomainError(
                    "work_reference_migration_git_preview_invalid",
                    "The reviewed Git rewrite contains an invalid file entry.",
                    status=409,
                )
            relative = str(raw.get("path") or "")
            source_hash = str(raw.get("source_hash") or "")
            target_hash = str(raw.get("target_hash") or "")
            if not relative or not source_hash or not target_hash or relative in expected:
                raise DomainError(
                    "work_reference_migration_git_preview_invalid",
                    "The reviewed Git rewrite contains an invalid or duplicate file entry.",
                    status=409,
                    details={"path": relative},
                )
            expected[relative] = raw

        current = {str(row["path"]): row for row in preview}
        unexpected = sorted(set(current) - set(expected))
        if unexpected:
            raise DomainError(
                "work_reference_migration_git_preview_changed",
                "New tracked reference changes appeared after this migration was reviewed.",
                status=409,
                details={"paths": unexpected},
            )
        tracked = set(_tracked_journal_files(repository, journal_home))
        selected: dict[str, Mapping[str, Any]] = {}
        for relative, expected_row in expected.items():
            if relative not in tracked:
                raise DomainError(
                    "work_reference_migration_git_preview_changed",
                    "A reviewed tracked file disappeared before migration.",
                    status=409,
                    details={"path": relative},
                )
            try:
                original = (repository / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise DomainError(
                    "work_reference_migration_git_preview_changed",
                    "A reviewed tracked file cannot be read before migration.",
                    status=409,
                    details={"path": relative},
                ) from exc
            observed_hash = content_hash(original)
            source_hash = str(expected_row["source_hash"])
            target_hash = str(expected_row["target_hash"])
            if observed_hash == target_hash:
                selected[relative] = {
                    "path": relative,
                    "content": original,
                }
                continue
            current_row = current.get(relative)
            if (
                observed_hash != source_hash
                or current_row is None
                or str(current_row.get("target_hash") or "") != target_hash
            ):
                raise DomainError(
                    "work_reference_migration_git_preview_changed",
                    "A reviewed tracked file changed before migration.",
                    status=409,
                    details={
                        "path": relative,
                        "expected_source_hash": source_hash,
                        "observed_hash": observed_hash,
                    },
                )
            selected[relative] = current_row

    for relative, row in selected.items():
        content = str(row["content"])
        path = repository / relative
        if content_hash(path.read_text(encoding="utf-8")) != content_hash(content):
            atomic_write_text(path, content)
    return tuple(sorted(selected))


def tracked_reference_rewrite_preview(
    repository: Path,
    journal_home: Path,
    mapping: Mapping[str, str],
) -> tuple[dict[str, str], ...]:
    """Describe exact tracked-journal rewrites without changing Git files."""

    if not mapping:
        return ()
    targets = {str(value) for value in mapping.values() if str(value)}
    replacements = {
        str(old): str(new)
        for old, new in mapping.items()
        if old and old != new and str(old) not in targets
    }
    if not replacements:
        return ()
    alternatives = "|".join(
        re.escape(value)
        for value in sorted(replacements, key=len, reverse=True)
    )
    version_pattern = (
        REFERENCE_STATE_VERSION_RE.pattern.removeprefix("^").removesuffix("$")
    )
    exact_reference = re.compile(
        rf"(?<![{WORK_REF_CONTINUATION}])(?P<source>{alternatives})"
        rf"(?P<version>:(?:{version_pattern}))?(?![{WORK_REF_CONTINUATION}])"
    )
    changed: list[dict[str, str]] = []
    for relative in _tracked_journal_files(repository, journal_home):
        path = repository / relative
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rewritten = exact_reference.sub(
            lambda match: (
                replacements[match.group("source")]
                + str(match.group("version") or "")
            ),
            original,
        )
        if rewritten == original:
            continue
        changed.append(
            {
                "path": relative,
                "source_hash": content_hash(original),
                "target_hash": content_hash(rewritten),
                "content": rewritten,
            }
        )
    return tuple(sorted(changed, key=lambda row: row["path"]))


def preview_tracked_reference_rewrites(
    journal_home: Path,
    mapping: Mapping[str, str],
) -> tuple[dict[str, str], ...]:
    """Return hashes and paths for the Git rewrite an operator reviews."""

    root = Path(journal_home).expanduser().resolve()
    repository = _repository_root(root)
    return tuple(
        {key: value for key, value in row.items() if key != "content"}
        for row in tracked_reference_rewrite_preview(
            repository,
            root,
            mapping,
        )
    )


def sync_plan(
    repository: Path,
    index: Mapping[str, Any],
    *,
    author: str = "",
    reference_mapping: Mapping[str, str] | None = None,
    reviewed_reference_files: Sequence[Mapping[str, Any]] | None = None,
) -> PlanSyncResult:
    """Write the index into the repository and commit only if it changed."""

    journal_home = Path(repository).expanduser().resolve()
    repository = _repository_root(journal_home)

    previous = _previous(journal_home)
    added, removed, changes, content_changes = _differences(previous, index)
    payload = plan_export_package(index)
    rows = list(payload["items"])

    rewritten_reference_files = _rewrite_tracked_references(
        repository,
        journal_home,
        reference_mapping or {},
        reviewed_files=reviewed_reference_files,
    )
    expected_nodes: set[str] = set()
    written_files = [PLAN_DOCUMENT, PLAN_DATA]
    atomic_write_text(journal_home / PLAN_DOCUMENT, render_plan_document(index))
    atomic_write_text(
        journal_home / PLAN_DATA,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    for row in rows:
        relative = node_document_path(row)
        expected_nodes.add(relative)
        written_files.append(relative)
        atomic_write_text(journal_home / relative, render_plan_node(row))
    node_root = journal_home / PLAN_NODE_ROOT
    if node_root.exists():
        for path in node_root.rglob("*.md"):
            relative = path.relative_to(journal_home).as_posix()
            if relative not in expected_nodes:
                path.unlink()

    plan_pathspec = _repo_relative(repository, journal_home / "plan")
    _git(repository, "add", "-A", "--", plan_pathspec)
    if rewritten_reference_files:
        _git(repository, "add", "--", *rewritten_reference_files)
    owned_pathspecs = (plan_pathspec, *rewritten_reference_files)
    staged = _git(
        repository,
        "diff",
        "--cached",
        "--name-only",
        "--",
        *owned_pathspecs,
    ).strip()
    if not staged:
        # Idempotent on purpose: a scheduled sync that commits every run fills
        # history with noise and trains everyone to ignore it.
        return PlanSyncResult(
            changed=False,
            committed="",
            item_count=len(rows),
            plan_revision=int(index.get("plan_revision") or 0),
            added=(),
            removed=(),
            status_changes=(),
            content_changes=(),
            written_files=(),
            rewritten_reference_files=(),
        )

    summary = f"plan: index at revision {int(index.get('plan_revision') or 0)}, {len(rows)} items"
    detail = []
    if added:
        detail.append("added " + ", ".join(added))
    if removed:
        detail.append("removed " + ", ".join(removed))
    if changes:
        detail.append("; ".join(changes))
    if content_changes:
        detail.append("content changed " + ", ".join(content_changes))
    if rewritten_reference_files:
        detail.append(
            f"rewrote plan references in {len(rewritten_reference_files)} tracked files"
        )
    message = summary + ("\n\n" + "\n".join(detail) if detail else "")
    commit_args = ["commit", "-m", message]
    if author:
        commit_args.extend(["--author", author])
    commit_args.extend(("--", *owned_pathspecs))
    _git(repository, *commit_args)
    committed = _git(repository, "rev-parse", "HEAD").strip()
    return PlanSyncResult(
        changed=True,
        committed=committed,
        item_count=len(rows),
        plan_revision=int(index.get("plan_revision") or 0),
        added=tuple(added),
        removed=tuple(removed),
        status_changes=tuple(changes),
        content_changes=tuple(content_changes),
        written_files=tuple(sorted(written_files)),
        rewritten_reference_files=rewritten_reference_files,
    )


__all__ = [
    "PLAN_DATA",
    "PLAN_DOCUMENT",
    "PLAN_NODE_ROOT",
    "PlanSyncResult",
    "plan_drift",
    "plan_export_package",
    "preview_tracked_reference_rewrites",
    "render_plan_document",
    "render_plan_node",
    "read_plan_export",
    "node_document_path",
    "sync_plan",
]

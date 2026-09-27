"""Where the project's files are in this agent's clones (W370).

Operator, 2026-09-27: a project's files (its instructions, facts,
environment, and any others the person lists) live in the project's
repositories, never in the board's database. The card lists each as a
repository alias and a path; three carry a purpose. `pb worker context`
turns the list into this agent's local paths, each with its state:

- ``present``: the file is in this agent's clone of that repository;
- ``missing``: the clone is here, the file is not (a branch behind, or a path
  the card names wrongly);
- ``not_cloned``: this agent has no clone of that repository yet.

The purpose files become ``project_instructions_ref``, ``project_facts_ref``
and ``project_environment_ref``, the names the skill already reads, so the
card, not the journal home, decides them once the board sends the list.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

FILE_STATES = ("present", "missing", "not_cloned")
PURPOSE_FIELDS = {
    "instructions": ("project_instructions_ref", "local_project_instructions", "project_instructions_state"),
    "facts": ("project_facts_ref", "local_project_facts", "project_facts_state"),
    "environment": ("project_environment_ref", "local_project_environment", "project_environment_state"),
}


def file_state(workspace: str, alias: str, path: str) -> tuple[str, str]:
    """(local path, state) of one listed file in this agent's clone."""

    if not workspace:
        return "", "not_cloned"
    clone = Path(workspace) / alias
    local = clone / path
    if not (clone / ".git").exists():
        return str(local), "not_cloned"
    return str(local), "present" if local.is_file() else "missing"


def listed_file(workspace: str, row: Mapping[str, Any]) -> dict[str, str]:
    alias = str(row.get("alias") or "")
    path = str(row.get("path") or "")
    local, state = file_state(workspace, alias, path)
    return {
        "purpose": str(row.get("purpose") or ""),
        "alias": alias,
        "path": path,
        "ref": f"repo:{alias}/{path}",
        "description": str(row.get("description") or ""),
        "local_path": local,
        "state": state,
    }


def files_context(workspace: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The context fields for a known files list: the purpose refs and the further files."""

    rows = [listed_file(workspace, row) for row in record.get("files") or [] if isinstance(row, Mapping)]
    fields: dict[str, Any] = {}
    for purpose, (ref_key, local_key, state_key) in PURPOSE_FIELDS.items():
        row = next((item for item in rows if item["purpose"] == purpose), None)
        # An empty slot is empty: the card is the source, never the journal home.
        fields[ref_key] = row["ref"] if row else ""
        fields[local_key] = row["local_path"] if row else ""
        fields[state_key] = row["state"] if row else ""
    fields["project_files"] = [
        {key: value for key, value in item.items() if key != "purpose"} for item in rows if not item["purpose"]
    ]
    fields["project_files_revision"] = int(record.get("files_revision") or 0)
    fields["project_files_editable"] = bool(record.get("edit_allowed"))
    return fields


def missing_files(workspace: str, record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every listed file that is not present in this agent's clones, purpose files included."""

    return [
        item
        for item in (listed_file(workspace, row) for row in record.get("files") or [] if isinstance(row, Mapping))
        if item["state"] != "present"
    ]


def files_fingerprint(workspace: str, record: Mapping[str, Any]) -> dict[str, str]:
    """Each listed file's content hash in this agent's clone ("" when it is not there), by its ref."""

    prints: dict[str, str] = {}
    for row in record.get("files") or []:
        if not isinstance(row, Mapping):
            continue
        item = listed_file(workspace, row)
        digest = ""
        if item["state"] == "present":
            try:
                digest = hashlib.sha256(Path(item["local_path"]).read_bytes()).hexdigest()
            except OSError:
                digest = ""
        prints[item["ref"]] = digest
    return prints


def changed_files(seen: Mapping[str, Any], record: Mapping[str, Any], now: Mapping[str, str]) -> list[dict[str, str]]:
    """What changed in the files since this agent last read them (W370: "told on their next check")."""

    before = seen.get("files") if isinstance(seen.get("files"), Mapping) else {}
    changes = [
        {"ref": ref, "change": "added" if ref not in before else "changed"}
        for ref, digest in now.items()
        if ref not in before or str(before.get(ref) or "") != digest
    ]
    changes += [{"ref": ref, "change": "removed"} for ref in before if ref not in now]
    if not changes and int(seen.get("files_revision") or 0) != int(record.get("files_revision") or 0):
        # The list changed (a description, an order, a purpose) with no file changing.
        changes.append({"ref": "", "change": "list"})
    return changes


__all__ = ["FILE_STATES", "changed_files", "files_fingerprint", "PURPOSE_FIELDS", "file_state", "files_context", "listed_file", "missing_files"]

from __future__ import annotations

import json
from pathlib import Path

from project_board.client.assignment_store import AssignmentStore


def _row(assignment_id: str, worker: str) -> dict:
    return {
        "assignment_id": assignment_id,
        "assignment_ref": f"work:assignment:20260925T010000Z:{assignment_id}:work",
        "worker_name": worker,
        "state": "assigned",
        "ownership_version": 1,
    }


def test_active_assignments_live_in_each_workers_pending_folder(tmp_path: Path):
    store = AssignmentStore(tmp_path / "assignments")
    store.write(_row("assignment_one", "Codex-UI"))
    store.write(_row("assignment_two", "Claude-Main"))

    assert (tmp_path / "assignments" / "codex-ui" / "pending" / "assignment_one.json").is_file()
    assert [row["assignment_id"] for row in store.list_pending(worker_name="codex-ui")] == ["assignment_one"]
    assert {row["assignment_id"] for row in store.list_pending()} == {"assignment_one", "assignment_two"}


def test_flat_assignment_migration_is_resumable_and_keeps_unreadable_input(tmp_path: Path):
    root = tmp_path / "assignments"
    root.mkdir()
    (root / "assignment_one.json").write_text(json.dumps(_row("assignment_one", "Codex-UI")))
    (root / "broken.json").write_text("{not json")
    store = AssignmentStore(root)

    result = store.migrate_legacy(batch_size=1)
    assert result["state"] == "running"
    result = store.migrate_legacy(batch_size=10)

    assert result["state"] == "complete"
    assert store.read("assignment_one", worker_name="codex-ui")["state"] == "assigned"
    assert (root / ".legacy-unreadable" / "assignments" / "broken.json").is_file()
    assert (root / "codex-ui" / ".migration.json").is_file()

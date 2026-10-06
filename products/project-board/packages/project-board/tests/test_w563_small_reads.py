"""W563 exchange observations 3 and 6 (Root, 2026-10-06, sha256 79c4e5ae; Ops verdicts 06:06 UTC)."""

from __future__ import annotations

import re

from project_board.client import cli
from project_board.client.procedures import source_package_path
from project_board.client.render import render_envelope
from project_board.contract.worker_operation_contract import PROBLEM_BOARD_OPERATIONS


def _index(items):
    return {"ok": True, "result": {"operation": "project.plan.index", "object": {
        "project_ref": "work:project:one", "matched_count": len(items), "items": items,
    }}}


def test_the_brief_index_shows_dependencies_or_none():
    # Observation 3: an exact one-item index showed the revision but not
    # depends_on, and the coordinator fell back to a JSON read.
    w461 = {"item_key": "W461", "status": "working", "revision": 280, "title": "Reconnect",
            "depends_on": ["work:plan:node:20261006T055234Z:w573:generic", "work:plan:node:20261006T055235Z:w574:receipts"]}
    alone = {"item_key": "W575", "status": "todo", "revision": 1, "title": "Alone", "depends_on": []}

    text = render_envelope(_index([w461, alone]))

    assert "W461 · working" in text and "depends on 2" in text
    assert "  depends_on: work:plan:node:20261006T055234Z:w573:generic, work:plan:node:20261006T055235Z:w574:receipts" in text
    assert "W575 · todo" in text and "depends on none" in text


def test_a_long_dependency_list_is_capped_with_a_count():
    item = {"item_key": "W1", "status": "todo", "revision": 1, "title": "Many",
            "depends_on": [f"work:plan:node:20261006T000000Z:w{index}:x" for index in range(12)]}

    text = render_envelope(_index([item]))

    assert "depends on 12" in text and "(+4 more)" in text


def test_every_command_in_the_handoff_map_exists():
    # Observation 6, Ops' condition: the map lives in the coordinator module,
    # not SKILL.md, and every command in it exists.
    module = (source_package_path() / "references/coordinator/write-the-recovery-handoff.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\\| [^|]+ \\| `pb (worker|coordinate) ([a-z.\\-]+)", module, flags=re.MULTILINE)
    assert len(rows) >= 10
    worker = cli.build_parser()._subparsers._group_actions[0].choices["worker"]
    worker_commands = worker._subparsers._group_actions[0].choices
    for family, name in rows:
        if family == "worker":
            assert name in worker_commands, f"pb worker {name}"
        else:
            assert name in PROBLEM_BOARD_OPERATIONS, f"pb coordinate {name}"
    assert "Act-to-command map" not in (source_package_path() / "SKILL.md").read_text(encoding="utf-8")

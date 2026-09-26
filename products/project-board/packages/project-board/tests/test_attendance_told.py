"""An unlinked agent is told (rehearsal gap 7, 2026-09-26).

After the operator unlinked an agent, nothing said so: receive only stopped
printing the project line, and context still served the project's record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
WORKER = "codex-11111111-1111-4111-8111-111111111111"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(
        worker_name=WORKER, runtime_kind="codex", runtime_session_id="11111111-1111-4111-8111-111111111111",
        capabilities=[], authority_label="authority:one",
    )
    store.listen_worker(WORKER)
    store.create_project(project_id=PROJECT, title="Leave", goal="Be told.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    return store


def _ended(result):
    return [signal for signal in result.get("signals") or [] if signal.get("kind") == "project.attendance_ended"]


def test_receive_names_a_project_that_dropped_out_since_the_last_receive(field):
    assert _ended(pull_worker_input(field, worker_name=WORKER, limit=1)) == []

    field.sync_worker_attendances(WORKER, [])  # the operator unlinks it
    [signal] = _ended(pull_worker_input(field, worker_name=WORKER, limit=1))
    assert signal["project_ref"] == PROJECT_REF
    assert "no longer attends" in signal["message"]

    assert _ended(pull_worker_input(field, worker_name=WORKER, limit=1)) == [], "told once"


def test_context_says_plainly_when_the_agent_does_not_attend():
    source = " ".join(Path(cli.__file__).read_text(encoding="utf-8").split())
    assert 'context["attending"] = str(args.project_ref) in attended' in source
    assert "work are not yours" in source

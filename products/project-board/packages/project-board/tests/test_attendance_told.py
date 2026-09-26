"""An unlinked agent is told (rehearsal gap 7, 2026-09-26).

After the operator unlinked an agent, nothing said so: receive only stopped
printing the project line, and context still served the project's record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.render import render_envelope
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


@pytest.mark.parametrize("listen_before_unlink", [True, False])
def test_a_listen_between_two_receives_does_not_hide_the_unlink(field, listen_before_unlink):
    # Every session start runs listen; the observed projects survive it.
    pull_worker_input(field, worker_name=WORKER, limit=1)
    if listen_before_unlink:
        field.listen_worker(WORKER)
    field.sync_worker_attendances(WORKER, [])
    if not listen_before_unlink:
        field.listen_worker(WORKER)
    [signal] = _ended(pull_worker_input(field, worker_name=WORKER, limit=1))
    assert signal["project_ref"] == PROJECT_REF


def test_brief_output_shows_the_unlink_signal(field):
    pull_worker_input(field, worker_name=WORKER, limit=1)
    field.sync_worker_attendances(WORKER, [])
    result = pull_worker_input(field, worker_name=WORKER, limit=1)
    text = render_envelope({"ok": True, "command": "worker.receive", "result": result})
    assert "SIGNAL project.attendance_ended:" in text
    assert f"project_ref = {PROJECT_REF}" in text
    assert "no longer attends" in text


def test_context_says_plainly_when_the_agent_does_not_attend():
    source = " ".join(Path(cli.__file__).read_text(encoding="utf-8").split())
    assert 'context["attending"] = str(args.project_ref) in attended' in source
    assert "work are not yours" in source

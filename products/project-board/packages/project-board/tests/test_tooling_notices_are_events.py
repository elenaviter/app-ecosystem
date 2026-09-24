"""Notices the tooling writes on a worker's behalf are events, not mail (W182).

Operator ruling, 2026-09-23. On dev-main 156 inbox rows were tooling notices
(notification path dead or restored, wake still queued or no longer queued,
`pb worker idle`): attributed to the worker as a message sender and indexed as
messages. They are project events carrying their facts now, and a new relay or
`pb` state notice routed as mail fails the guard below.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client import relay as relay_module
from project_board.client.store import SharedFieldStore


CLIENT_ROOT = Path(relay_module.__file__).resolve().parent
WORKER = "claude-code-abc"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="claude-code", capabilities=[], authority_label="authority:abc")
    store.create_project(project_id=PROJECT, title="Notices", goal="Notices are events.", owner="operator")
    return store


def test_a_keyed_event_is_queued_once_and_its_receipt_says_so(field):
    first = field.enqueue_service_event(
        PROJECT,
        worker_name=WORKER,
        kind="worker.notification_path",
        summary="fable-pub: notification path dead. The relay reports it.",
        source_event_ref="relay:dead-path:abc",
        metadata={"state": "dead"},
        idempotency_key="dead-path:abc",
    )
    again = field.enqueue_service_event(
        PROJECT,
        worker_name=WORKER,
        kind="worker.notification_path",
        summary="a retry with anything",
        source_event_ref="relay:dead-path:abc",
        metadata={"state": "dead"},
        idempotency_key="dead-path:abc",
    )

    assert again["replayed"] is True
    assert again["outbox_id"] == first["outbox_id"]
    assert field.service_event_receipt_exists(worker_name=WORKER, idempotency_key="dead-path:abc")
    assert not field.service_event_receipt_exists(worker_name=WORKER, idempotency_key="other")
    rows = field.pull_outbox(relay_id="relay", worker_name=WORKER, project_ref=f"work:project:{PROJECT}", limit=10)
    events = [row for row in rows if row["kind"] == "event.publish"]
    assert len(events) == 1 and events[0]["payload"]["kind"] == "worker.notification_path"
    stores = {(store, agent) for store, agent, _path, _days in maintenance.keyed_stores(field)}
    assert ("idempotency-events", WORKER) in stores


class _Field:
    def __init__(self, reach: dict) -> None:
        self.reach = reach
        self.record: dict = {}
        self.events: list[dict] = []

    def worker_reachability(self, worker_name):
        return dict(self.reach)

    def dead_path_record(self, worker_name):
        return dict(self.record)

    def write_dead_path_record(self, worker_name, record):
        self.record = dict(record) if record else {}

    def service_event_receipt_exists(self, *, worker_name, idempotency_key):
        return any(event["idempotency_key"] == idempotency_key for event in self.events)

    def enqueue_service_event(self, project_id, **event):
        self.events.append({"project_id": project_id, **event})
        return {"queued": True}


def _adapter(reach: dict, *, project_id: str):
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        runtime_kind="claude-code",
        worker_name=WORKER,
        worker_alias="fable-pub",
        runtime_session_id="0247bb87-0b0f-4009-aadb-e2df67c509fe",
        project_id=project_id,
    )
    adapter.field = _Field(reach)
    return adapter


DEAD = {
    "state": "not_listening",
    "session_state": "working",
    "pending_messages": 2,
    "last_inbox_check_at": "2026-09-24T06:00:00Z",
    "overdue_by_seconds": 3600,
}


def test_a_worker_on_no_project_publishes_no_event_and_its_card_carries_the_state():
    adapter = _adapter(DEAD, project_id="")

    result = adapter._report_dead_notification_path()

    assert result["reported"] is True
    assert adapter.field.events == [], "no project to publish to, and never mail"
    assert adapter.field.record["open_note"] == "enqueued"


def test_an_open_event_carries_the_pending_count_frozen_at_the_open():
    adapter = _adapter(DEAD, project_id=PROJECT)
    adapter.field.record = {
        "kind": "dead_path",
        "wake_id": "",
        "since": "2026-09-24T06:00:00Z",
        "open_note": "pending",
        "pending": 2,
    }
    adapter.field.reach = {**DEAD, "pending_messages": 9}

    adapter._report_dead_notification_path()

    [event] = adapter.field.events
    assert event["metadata"]["pending_messages"] == 2, "a retry replays the open's facts"
    assert "2 message(s) waiting" in event["summary"]
    assert event["metadata"]["state"] == "dead"
    assert event["metadata"]["reported_by"] == "relay"


# A delivery-failure report tells the operator that a message a worker sent
# could not be delivered. It is about that message, which is mail, not a notice
# the tooling writes about a worker.
MAIL_TO_OPERATOR_ALLOWED = {("store.py", "report_mail_delivery_failure")}


def _operator_mail_calls(path: Path) -> list[tuple[str, int]]:
    """(enclosing function, line) of calls that send mail to 'operator'."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def visit(node, function=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                name = getattr(child.func, "attr", "") or getattr(child.func, "id", "")
                if name in {"enqueue_remote_mail", "send_mail"} and any(
                    keyword.arg == "recipient"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "operator"
                    for keyword in child.keywords
                ):
                    found.append((function, child.lineno))
            visit(child, function)

    visit(tree)
    return found


def test_no_relay_or_pb_state_notice_is_routed_as_mail():
    offenders = {
        path.name: calls
        for path in sorted(CLIENT_ROOT.glob("*.py"))
        for calls in [[
            (function, line) for function, line in _operator_mail_calls(path)
            if (path.name, function) not in MAIL_TO_OPERATOR_ALLOWED
        ]]
        if calls
    }
    assert offenders == {}, f"tooling notices must be events, not mail: {offenders}"

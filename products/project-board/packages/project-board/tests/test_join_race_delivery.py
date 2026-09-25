"""Mail sent right after a link reaches the new agent (W304, join race, 2026-09-25 14:06Z).

The board linked codex-coord and issued its welcome at 14:06:37; the relay
refused it at 14:06:41 as "not linked", because the host's record of the
worker's attendance was observed at about 14:03, before the link. The next
attendance poll came at 14:07:47. Newest evidence wins now: the local
"not linked" refuses only when it was observed at or after the message was
created, and a delivery made on the board's newer link asks the relay to
read the attendance at once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from project_board.client import relay as relay_module
from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from relay_helpers import make_host

PROJECT_ID = "quickstart-works-mttfmgqu"
PROJECT_REF = f"work:project:{PROJECT_ID}"
COORDINATOR = "claude-code-dfd0d696-82d2-4bec-8a9c-d94493ec63a5"


def _host(tmp_path, *, observed_at: str, attends: bool):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    field.listen_worker(identity.worker_name)
    field.create_project(project_id=PROJECT_ID, title="Quickstart works", goal="Onboard agents.", owner="operator")
    field.sync_worker_attendances(identity.worker_name, [PROJECT_REF] if attends else [])
    # Pin when this host last observed the worker's attendance.
    path = field._worker_path(identity.worker_name)  # noqa: SLF001 - the evidence under test
    row = __import__("json").loads(path.read_text(encoding="utf-8"))
    row["attendances_observed_at"] = observed_at
    path.write_text(__import__("json").dumps(row), encoding="utf-8")
    return identity, field


def _welcome(recipient: str, *, created_at: str, number: int = 1) -> dict:
    payload = {"mail": {"kind": "request", "subject": "You joined Quickstart works as worker", "body": "Welcome."}}
    return {
        "ref": f"work:control:command_{number:032d}",
        "kind": "mail",
        "project_ref": PROJECT_REF,
        "recipient": recipient,
        "sender": COORDINATOR,
        "subject": "You joined Quickstart works as worker",
        "payload": payload,
        "payload_hash": content_hash(payload),
        "created_at": created_at,
    }


def test_a_welcome_created_after_the_last_observation_is_delivered(tmp_path):
    identity, field = _host(tmp_path, observed_at="2026-09-25T14:03:00Z", attends=False)
    receipt = field.materialize_control(_welcome(identity.worker_name, created_at="2026-09-25T14:06:37Z"))
    assert receipt["delivery_status"] == "pending"
    assert receipt["attendance_lagging"] is True


def test_an_assignment_right_after_the_link_is_delivered_too(tmp_path):
    identity, field = _host(tmp_path, observed_at="2026-09-25T14:03:00Z", attends=False)
    receipt = field.send_assignment_notice(
        PROJECT_ID,
        assignment={
            "assignment_ref": "work:assignment:20260925T140640000000Z:assignment_0123456789abcdef0123456789abcdef:first-task",
            "ownership_version": 1,
            "identity_ref": "work:plan:node:20260925T140000Z:w999:first-task",
        },
        recipient=identity.worker_name,
        sender_identity=None,
        evidence_at="2026-09-25T14:06:40Z",
    )
    assert receipt["delivery_status"] == "pending"


def test_an_unlink_observed_after_the_message_still_refuses(tmp_path):
    identity, field = _host(tmp_path, observed_at="2026-09-25T14:08:00Z", attends=False)
    with pytest.raises(DomainError) as refused:
        field.materialize_control(_welcome(identity.worker_name, created_at="2026-09-25T14:06:37Z"))
    assert refused.value.code == "field_worker_not_linked"


def test_a_message_without_a_creation_time_keeps_the_local_answer(tmp_path):
    identity, field = _host(tmp_path, observed_at="2026-09-25T14:03:00Z", attends=False)
    control = _welcome(identity.worker_name, created_at="")
    with pytest.raises(DomainError) as refused:
        field.materialize_control(control)
    assert refused.value.code == "field_worker_not_linked"


def test_the_relay_reads_the_attendance_at_once_after_such_a_delivery():
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        project_id=PROJECT_ID, relay_id="relay-1", worker_name="claude-code-new",
        reconcile_ceiling_seconds=30, allow_session_resume_view=False,
        allowed_control_kinds=("mail", "request", "reply"), max_control_bytes=65536,
        allowed_peer_workers=("*",),
    )
    adapter._attendance_cache = {"initialized": True, "items": []}
    calls = []

    class Client:
        async def action(self, *, object_ref, action, payload):
            calls.append(action)
            if action == "control.pull":
                return {"lease_id": "lease-1", "items": [_welcome("claude-code-new", created_at="2026-09-25T14:06:37Z")]}
            return {"applied": True}

    class Field:
        def materialize_control(self, item):
            return {"delivery_status": "pending", "message_ref": "work:mail:x", "attendance_lagging": True}

    adapter.client = Client()
    adapter.field = Field()

    async def no_attachments(item):
        return None

    adapter._fetch_attachments = no_attachments
    counts = asyncio.run(adapter._pull_controls())
    assert counts["controls_materialized"] == 1
    assert adapter._attendance_cache["initialized"] is False

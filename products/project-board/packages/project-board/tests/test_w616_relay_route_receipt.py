"""A routed mail's Board receipt survives the relay drain without private detail."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.client.io import new_id, utc_now
from project_board.client.store import SharedFieldStore


WORKER = "codex-route-receipt"
PROJECT_ID = "w616-route-receipt"
PROJECT_REF = f"work:project:{PROJECT_ID}"


class _BoardClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def action(self, **kwargs):
        self.calls.append(kwargs)
        return {"object": self.answer}


def _fixture(tmp_path, answer, *, kind="mail.route"):
    field = SharedFieldStore(tmp_path / "field")
    field.initialize(field_id="w616-route-receipt-test")
    field.register_worker(
        worker_name=WORKER,
        runtime_kind="codex",
        capabilities=[],
        authority_label=f"authority:{WORKER}",
    )
    field.create_project(
        project_id=PROJECT_ID,
        title="W616 route receipt",
        goal="Keep the Board's bounded receipt.",
        owner="operator",
    )
    field.sync_worker_attendances(WORKER, [PROJECT_REF])
    if kind == "mail.route":
        queued = field.enqueue_remote_mail(
            PROJECT_ID,
            sender=WORKER,
            recipient="operator",
            kind="update",
            subject="Status",
            body="Done.",
            idempotency_key="mail-route-receipt",
        )
    else:
        queued = {"outbox_id": new_id("outbox_test")}
        field._outbox.write_pending({
            "outbox_id": queued["outbox_id"],
            "kind": kind,
            "worker_name": WORKER,
            "project_ref": PROJECT_REF,
            "object_ref": PROJECT_REF,
            "payload": {"kind": "test"},
            "state": "pending",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        })
    client = _BoardClient(answer)
    adapter = relay.ProblemBoardHostRelayAdapter.__new__(
        relay.ProblemBoardHostRelayAdapter
    )
    adapter.config = SimpleNamespace(
        relay_id="w616-test-relay",
        worker_name=WORKER,
        project_id=PROJECT_ID,
    )
    adapter.field = field
    adapter.client = client
    adapter._outbox_finishes = set()
    return field, adapter, client, queued["outbox_id"]


def _drain(field, adapter, outbox_id):
    counts = asyncio.run(adapter._flush_outbox_unlocked())
    assert counts["outbox_sent"] == 1
    row = field._outbox.read(
        outbox_id, worker_name=WORKER, project_ref=PROJECT_REF
    )
    assert row["state"] == "sent"
    status = field.worker_outbox_status(worker_name=WORKER, outbox_id=outbox_id)
    return row, status


def test_mail_route_receipt_is_projected_from_the_real_relay_drain(tmp_path):
    answer = {
        "disposition": "accepted",
        "routed_to": "thread_writer",
        "notification": {
            "state": "sent",
            "detail": "private recipient and transport details",
        },
        "private_route": {"operator": "someone-else"},
    }
    field, adapter, client, outbox_id = _fixture(tmp_path, answer)

    row, status = _drain(field, adapter, outbox_id)

    assert client.calls[0]["action"] == "mail.route"
    assert row["remote_result"] == {
        "disposition": "accepted",
        "routed_to": "thread_writer",
        "notification": {"state": "sent"},
    }
    assert status["remote_disposition"] == "accepted"
    assert status["routed_to"] == "thread_writer"
    assert status["notification"] == {"state": "sent"}
    assert "private" not in str(row) and "private" not in str(status)


@pytest.mark.parametrize(
    "answer,expected_proof",
    [
        ({"disposition": "accepted"}, {"disposition": "accepted"}),
        (
            {"disposition": "accepted", "routed_to": {"private": "route"},
             "notification": ["private"]},
            {"disposition": "accepted"},
        ),
        (
            {"disposition": "accepted", "routed_to": "secret-route",
             "notification": {"state": "private-state", "detail": "secret"}},
            {"disposition": "accepted"},
        ),
        (
            {"disposition": "accepted", "notification": {
                "state": "", "detail": "secret-detail-token",
            }},
            {"disposition": "accepted", "notification": {"state": "not_requested"}},
        ),
        (
            {"disposition": "accepted", "notification": {
                "detail": "secret-detail-token",
            }},
            {"disposition": "accepted", "notification": {"state": "not_requested"}},
        ),
    ],
    ids=["no-notification", "malformed", "unknown-values", "empty-state", "missing-state"],
)
def test_mail_route_receipt_omits_untrusted_or_absent_details(
    tmp_path, answer, expected_proof
):
    field, adapter, _client, outbox_id = _fixture(tmp_path, answer)

    row, status = _drain(field, adapter, outbox_id)

    assert row["remote_result"] == expected_proof
    assert status.get("routed_to") == expected_proof.get("routed_to")
    assert status.get("notification") == expected_proof.get("notification")
    assert "secret" not in str(row) and "secret" not in str(status)


@pytest.mark.parametrize(
    "kind", ["event.publish", "control.worker_settle", "assignment.report"]
)
def test_non_mail_route_does_not_store_a_spoofed_receipt(tmp_path, kind):
    answer = {
        "disposition": "accepted",
        "routed_to": "thread_writer",
        "notification": {"state": "sent"},
    }
    field, adapter, client, outbox_id = _fixture(tmp_path, answer, kind=kind)

    row, status = _drain(field, adapter, outbox_id)

    assert client.calls[0]["action"] == kind
    assert row.get("remote_result") is None
    assert "routed_to" not in status and "notification" not in status

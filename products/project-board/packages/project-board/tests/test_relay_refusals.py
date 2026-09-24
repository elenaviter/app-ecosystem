"""A refused control is logged, tells its sender, and never closes the channel (W304 finding 38).

On 2026-09-24 claude-ops on spark1 refused every project mail from claude-main.
The receiver policy denied the peer (a new host allows no peers), the relay
logged nothing, the sender was not told, and the board's receipt for the
applied refusal (the control, state "refused") was read as a refused
operation, which closed the worker channel each time.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from project_board.client import relay as relay_module
from project_board.contract.errors import DomainError
from project_board.contract.operation_outcomes import require_applied_operation_outcome

SENDER = "claude-code-dfd0d696-82d2-4bec-8a9c-d94493ec63a5"


def _misread_refusal() -> DomainError:
    # Exactly what the client raised on spark1: the receipt of an applied
    # refusal is the control, and its own state reads as the outcome.
    try:
        require_applied_operation_outcome(
            "control.refuse", {"object_kind": "work.control", "state": "refused"}
        )
    except DomainError as exc:
        return exc
    raise AssertionError("the misreading is the premise of this test")


class Client:
    def __init__(self, items, refuse_error=None):
        self.items = items
        self.refuse_error = refuse_error
        self.calls = []

    async def action(self, *, object_ref, action, payload):
        self.calls.append({"object_ref": object_ref, "action": action, "payload": payload})
        if action == "control.pull":
            return {"lease_id": "lease-1", "items": self.items}
        if action == "control.refuse" and self.refuse_error is not None:
            raise self.refuse_error
        return {"applied": True}


def _adapter(client, *, peers=()):
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        project_id="quickstart-works-mttfmgqu",
        relay_id="relay-spark1-claude-ops",
        worker_name="claude-code-a7b7935d-a064-43ec-937e-2b94f1660b68",
        reconcile_ceiling_seconds=30,
        allow_session_resume_view=False,
        allowed_control_kinds=("mail", "ping", "request", "reply"),
        max_control_bytes=65536,
        allowed_peer_workers=tuple(peers),
    )
    adapter.client = client
    return adapter


def _mail(number: int) -> dict:
    return {
        "ref": f"work:control:command_{number}",
        "kind": "mail",
        "sender": SENDER,
        "payload": {"subject": "Onboarding check 2", "body": "reply please"},
        "created_at": f"2026-09-24T20:39:4{number}Z",
    }


def _pull(adapter):
    return asyncio.run(adapter._pull_controls())


def test_a_peer_denied_mail_is_logged_and_its_sender_is_told(caplog):
    client = Client([_mail(1)])

    with caplog.at_level(logging.WARNING):
        counts = _pull(_adapter(client))

    assert counts["controls_refused"] == 1
    [refusal] = [call for call in client.calls if call["action"] == "control.refuse"]
    failure = refusal["payload"]["result"]["delivery_failure"]
    assert failure["code"] == "receiver_policy_peer_denied"
    assert failure["field"] == "receiver_policy.allowed_peer_workers"
    assert failure["value"] == SENDER
    assert "pb host configure --allow-peer-worker" in failure["reason"]
    [line] = [r.getMessage() for r in caplog.records if "relay refused control" in r.getMessage()]
    assert "control=work:control:command_1" in line
    assert f"sender={SENDER}" in line
    assert "receiver_policy_peer_denied" in line


def test_an_applied_refusal_read_as_refused_does_not_stop_the_batch():
    client = Client([_mail(1), _mail(2)], refuse_error=_misread_refusal())

    counts = _pull(_adapter(client))

    assert [call["object_ref"] for call in client.calls if call["action"] == "control.refuse"] == [
        "work:control:command_1",
        "work:control:command_2",
    ]
    assert counts["controls_refused"] == 2


def test_a_settle_the_board_rejects_is_logged_and_the_batch_goes_on(caplog):
    stale = DomainError("work_control_lease_stale", "The control lease is no longer held.", status=409)
    client = Client([_mail(1), _mail(2)], refuse_error=stale)

    with caplog.at_level(logging.WARNING):
        _pull(_adapter(client))

    assert sum(call["action"] == "control.refuse" for call in client.calls) == 2
    failures = [r.getMessage() for r in caplog.records if "could not settle control" in r.getMessage()]
    assert len(failures) == 2
    assert "code=work_control_lease_stale" in failures[0]


def test_an_allowed_peer_is_not_refused():
    client = Client([_mail(1)])
    adapter = _adapter(client, peers=("*",))

    assert adapter._receiver_refusal(_mail(1)) == ""


def test_the_sender_sees_its_own_delivery_refused_with_the_reason(tmp_path):
    """End to end on the sender's host (codex-ui's review of the finding 38 pair).

    The sender's relay routed the mail and recorded it sent. The receiving
    host refused it by receiver policy, and the board answered the sender
    with a delivery_failed notice. `pb worker deliveries` must show the
    original delivery refused, with the code, setting, value and reason.
    """

    from project_board.client.io import content_hash
    from project_board.client.store import SharedFieldStore

    sender = SENDER
    project = "quickstart-works-mttfmgqu"
    field = SharedFieldStore(tmp_path / "field")
    field.initialize()
    field.register_worker(worker_name=sender, runtime_kind="claude-code", capabilities=[], authority_label="authority:claude-code")
    field.create_project(project_id=project, title="Quickstart works", goal="Onboard agents.", owner="operator")

    field.sync_project_mail_recipients(project, [{"worker_name": "claude-code-a7b7935d-a064-43ec-937e-2b94f1660b68", "worker_alias": "claude-ops"}])

    # 1. The sender queues mail for claude-ops, and its relay routes it: the board accepts, so the row is sent.
    queued = field.enqueue_remote_mail(
        project,
        sender=sender,
        recipient="claude-code-a7b7935d-a064-43ec-937e-2b94f1660b68",
        kind="request",
        subject="Onboarding check 2",
        body="reply please",
        idempotency_key="check-2",
    )
    [row] = field.pull_outbox(relay_id="relay-dev-main", worker_name=sender)
    source_ref = row["payload"]["source_message_ref"]
    field.settle_outbox(row["outbox_id"], relay_id="relay-dev-main", outcome="sent", remote_ref="work:control:command_1")
    assert field.list_mail_deliveries(worker_name=sender, states=["refused"])["items"] == []

    # 2. The receiving host refuses it by its receiver policy.
    receiving = _adapter(Client([]))
    control = {**_mail(1), "payload": {"mail": {"kind": "request", "subject": "Onboarding check 2", "source_message_ref": source_ref}}}
    failure = receiving._receiver_policy_failure(control, "receiver_policy_peer_denied")
    assert failure["source_message_ref"] == source_ref

    # 3. The board answers the sender with a delivery_failed notice, and the sender's relay materializes it.
    notice_payload = {"mail": {"kind": "delivery_failed", "subject": "Problem Board rejected part of a message",
                               "body": "Receiver policy refused it.", "payload": {"delivery_failure": failure}}}
    field.materialize_control({
        "ref": "work:control:command_notice",
        "kind": "mail",
        "subject": "Problem Board rejected part of a message",
        "project_ref": f"work:project:{project}",
        "recipient": sender,
        "sender": "claude-code-a7b7935d-a064-43ec-937e-2b94f1660b68",
        "payload": notice_payload,
        "payload_hash": content_hash(notice_payload),
    })

    [refused] = field.list_mail_deliveries(worker_name=sender, states=["refused"])["items"]
    assert refused["outbox_id"] == row["outbox_id"]
    assert refused["recipient"] == "claude-code-a7b7935d-a064-43ec-937e-2b94f1660b68"
    assert refused["kind"] == "request"
    assert refused["subject"] == "Onboarding check 2"
    assert refused["delivery_failure"]["code"] == "receiver_policy_peer_denied"
    assert refused["delivery_failure"]["field"] == "receiver_policy.allowed_peer_workers"
    assert refused["delivery_failure"]["value"] == SENDER
    assert "pb host configure --allow-peer-worker" in refused["delivery_failure"]["reason"]
    assert queued

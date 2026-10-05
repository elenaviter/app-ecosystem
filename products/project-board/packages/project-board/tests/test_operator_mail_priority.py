"""Trusted operator replies preempt a bounded worker-mail backlog (W433)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client import store as store_module
from project_board.client.io import content_hash
from project_board.client.mail_budget import MAX_WORKER_INPUT_BYTES
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError


PROJECT = "priority-project"
WORKER = "claude-main"
SENDER = "codex-app"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="priority-test")
    for name in (WORKER, SENDER):
        store.register_worker(
            worker_name=name,
            runtime_kind="codex",
            capabilities=[],
            authority_label=f"authority:{name}",
        )
    store.create_project(
        project_id=PROJECT,
        title="Priority",
        goal="Operator replies reach a bounded receive promptly.",
        owner="operator",
    )
    return store


def _worker_mail(field: SharedFieldStore, number: int, *, claimed_user: bool = False) -> dict:
    return field.send_mail(
        PROJECT,
        sender=SENDER,
        recipient=WORKER,
        kind="reply" if claimed_user else "update",
        subject=f"Worker update {number}",
        body="Progress.",
        idempotency_key=f"worker-update-{number}",
        sender_identity={"kind": "user", "label": "Operator"} if claimed_user else None,
    )


def _operator_reply(field: SharedFieldStore) -> dict:
    payload = {"body": "Approved.", "correlation_id": "approval-thread"}
    return field.materialize_control(
        {
            "ref": "work:control:20260930T222600Z:command_approval:w433-approval",
            "project_ref": f"work:project:{PROJECT}",
            "recipient": WORKER,
            "kind": "reply",
            "subject": "Backup root approved",
            "payload": payload,
            "payload_hash": content_hash(payload),
            "sender_identity": {"kind": "user", "label": "Operator"},
        }
    )


def _legacy_id_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put an old random-ID operator mail behind the worker IDs deterministically."""

    original = store_module.new_id
    ids = iter([f"mail_{number:032x}" for number in range(5)] + [f"mail_{15:032x}"])

    def next_id(prefix: str) -> str:
        return next(ids) if prefix == "mail" else original(prefix)

    monkeypatch.setattr(store_module, "new_id", next_id)


def test_operator_reply_is_first_in_bounded_receive_and_worker_mail_remains(field, monkeypatch):
    _legacy_id_order(monkeypatch)
    workers = [_worker_mail(field, number) for number in range(5)]
    approval = _operator_reply(field)

    first = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=3)
    assert len(first) == 3
    assert first[0]["message_ref"] == approval["message_ref"]
    assert first[0]["sender_identity"]["kind"] == "user"
    assert all(row["lease"]["lease_id"] for row in first)

    second = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=3)
    assert len(second) == 3
    assert {row["message_ref"] for row in first + second} == {
        approval["message_ref"],
        *(row["message_ref"] for row in workers),
    }
    assert field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=3) == []

    operator_row = first[0]
    visible_reply = field.enqueue_remote_mail(
        PROJECT,
        sender=WORKER,
        recipient="operator",
        kind="reply",
        subject="Re: Backup root approved",
        body="Approval received.",
        correlation_id=operator_row["correlation_id"],
        reply_to=operator_row["message_ref"],
        idempotency_key="approval-visible-reply",
    )
    settled_approval = field.settle_mail(
        PROJECT,
        worker_name=WORKER,
        message_ref=operator_row["message_ref"],
        lease_id=operator_row["lease"]["lease_id"],
        lease_owner="session",
        outcome="acknowledged",
    )
    assert settled_approval["operator_response_outbox_id"] == visible_reply["outbox_id"]
    with pytest.raises(DomainError):
        field.settle_mail(
            PROJECT,
            worker_name=WORKER,
            message_ref=operator_row["message_ref"],
            lease_id=operator_row["lease"]["lease_id"],
            lease_owner="session",
            outcome="acknowledged",
        )

    worker_row = next(row for row in first + second if row["message_ref"] == workers[0]["message_ref"])
    field.settle_mail(
        PROJECT,
        worker_name=WORKER,
        message_ref=worker_row["message_ref"],
        lease_id=worker_row["lease"]["lease_id"],
        lease_owner="session",
        outcome="acknowledged",
    )
    with pytest.raises(DomainError):
        field.settle_mail(
            PROJECT,
            worker_name=WORKER,
            message_ref=worker_row["message_ref"],
            lease_id=worker_row["lease"]["lease_id"],
            lease_owner="session",
            outcome="acknowledged",
        )


def test_claimed_user_identity_on_worker_mail_does_not_gain_priority(field, monkeypatch):
    _legacy_id_order(monkeypatch)
    spoof = _worker_mail(field, 1, claimed_user=True)
    approval = _operator_reply(field)

    first = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1)
    assert first[0]["message_ref"] == approval["message_ref"]
    second = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1)
    assert second[0]["message_ref"] == spoof["message_ref"]


def test_routed_worker_reply_cannot_gain_priority_from_a_user_identity(field, monkeypatch):
    _legacy_id_order(monkeypatch)
    routed_payload = {
        "mail": {
            "kind": "reply",
            "subject": "Worker reply",
            "body": "Progress.",
            "payload": {},
        }
    }
    routed = field.materialize_control(
        {
            "ref": "work:control:20260930T222601Z:command_routed:w433-worker-reply",
            "project_ref": f"work:project:{PROJECT}",
            "recipient": WORKER,
            "sender": SENDER,
            "kind": "mail",
            "subject": "Worker reply",
            "payload": routed_payload,
            "payload_hash": content_hash(routed_payload),
            "sender_identity": {"kind": "user", "label": "Operator"},
        }
    )
    approval = _operator_reply(field)

    first = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1)
    assert first[0]["message_ref"] == approval["message_ref"]
    second = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1)
    assert second[0]["message_ref"] == routed["message_ref"]


def test_worker_receive_limit_three_surfaces_approval_and_pages_remaining_mail(field, monkeypatch):
    _legacy_id_order(monkeypatch)
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    field.listen_worker(WORKER)
    workers = [_worker_mail(field, number) for number in range(4)]
    approval = _operator_reply(field)

    first = pull_worker_input(field, worker_name=WORKER, limit=3)
    first_items = [item["message"] for item in first["items"]]
    assert len(first_items) == 3
    assert first_items[0]["message_ref"] == approval["message_ref"]
    assert first["items"][0]["operator_response"]["required_before_settlement"]
    assert first["delivery"]["remaining_count"] == 2
    assert first["delivery"]["has_more"] is True

    second = pull_worker_input(field, worker_name=WORKER, limit=3)
    second_items = [item["message"] for item in second["items"]]
    assert len(second_items) == 2
    assert second["delivery"]["remaining_count"] == 0
    assert second["delivery"]["has_more"] is False
    assert {row["message_ref"] for row in first_items + second_items} == {
        approval["message_ref"],
        *(row["message_ref"] for row in workers),
    }


@pytest.mark.parametrize("kind", ["request", "reply"])
def test_priority_and_selective_receive_share_admission_and_settle_once(field, monkeypatch, kind):
    _legacy_id_order(monkeypatch)
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    field.listen_worker(WORKER)
    older = _worker_mail(field, 0)
    payload = {"body": "Approved.", "correlation_id": "intersection-thread"}
    operator = field.materialize_control({
        "ref": f"work:control:intersection-{kind}",
        "project_ref": f"work:project:{PROJECT}",
        "recipient": WORKER, "kind": kind, "subject": "Operator input",
        "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })
    assert operator["message_id"].startswith("mail-priority_")
    assert field._mail_record_unlocked(PROJECT, WORKER, operator["message_ref"])["admitted_operator_control"] is True

    selected = pull_worker_input(field, worker_name=WORKER, message_ref=older["message_ref"])
    assert selected["items"] == []
    assert selected["selection"]["state"] == "operator_pending"
    assert field._mail_record_unlocked(PROJECT, WORKER, older["message_ref"])["state"] == "pending"

    ordinary = pull_worker_input(field, worker_name=WORKER, limit=1)
    assert len(ordinary["items"]) == 1
    item = ordinary["items"][0]
    assert item["message"]["message_ref"] == operator["message_ref"]
    assert ordinary["delivery"]["remaining_count"] == 1
    assert ordinary["session"]["general_receive_due"] is False
    field.enqueue_remote_mail(
        PROJECT, sender=WORKER, recipient="operator", kind="reply",
        subject="Re: Operator input", body="Received.",
        correlation_id=item["message"]["correlation_id"],
        reply_to=operator["message_ref"], idempotency_key=f"intersection-reply-{kind}",
    )
    settle = dict(
        worker_name=WORKER, message_ref=operator["message_ref"],
        lease_id=item["message"]["lease"]["lease_id"], lease_owner=WORKER,
        outcome="acknowledged", summary="Replied.",
    )
    field.settle_mail(PROJECT, **settle)
    with pytest.raises(DomainError):
        field.settle_mail(PROJECT, **settle)

    selected = pull_worker_input(field, worker_name=WORKER, message_ref=older["message_ref"])
    assert [row["message"]["message_ref"] for row in selected["items"]] == [older["message_ref"]]
    # W563 (Q11): the first of three selective receives leaves two.
    assert selected["session"]["general_receive_due"] is False
    assert selected["session"]["selective_receives_since_general"] == 1


def test_priority_materialization_replay_does_not_duplicate_or_reidentify_mail(field):
    operator = _operator_reply(field)
    replay = _operator_reply(field)
    assert replay["replayed"] is True
    assert replay["message_id"] == operator["message_id"]
    assert replay["message_ref"] == operator["message_ref"]
    first = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1)
    assert [row["message_ref"] for row in first] == [operator["message_ref"]]
    assert field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session", limit=1) == []


def test_worker_cannot_assert_the_shared_operator_admission_predicate(field, monkeypatch):
    _legacy_id_order(monkeypatch)
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    field.listen_worker(WORKER)
    spoof = field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind="request", subject="Spoof",
        body="Operator label is not authority.", idempotency_key="admission-spoof",
        sender_identity={"kind": "user", "label": "Operator"},
        admitted_operator_control=True,
    )
    assert spoof["message_id"].startswith("mail_")
    assert field._mail_record_unlocked(PROJECT, WORKER, spoof["message_ref"]).get("admitted_operator_control") is not True
    selected = pull_worker_input(field, worker_name=WORKER, message_ref=spoof["message_ref"])
    assert [row["message"]["message_ref"] for row in selected["items"]] == [spoof["message_ref"]]


def test_direct_mail_still_precedes_priority_mail_in_the_project(field):
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    field.listen_worker(WORKER)
    project_operator = _operator_reply(field)
    payload = {"body": "Direct operator input."}
    direct_operator = field.materialize_control({
        "ref": "work:control:direct-operator-input", "recipient": WORKER,
        "kind": "request", "subject": "Direct input",
        "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })
    first = pull_worker_input(field, worker_name=WORKER, limit=1)
    assert first["items"][0]["scope"] == "direct"
    assert first["items"][0]["message"]["message_ref"] == direct_operator["message_ref"]
    assert first["delivery"]["remaining_count"] == 1
    assert field._mail_record_unlocked(PROJECT, WORKER, project_operator["message_ref"])["state"] == "pending"
    second = pull_worker_input(field, worker_name=WORKER, limit=1)
    assert second["items"][0]["message"]["message_ref"] == project_operator["message_ref"]


def test_priority_does_not_bypass_the_response_byte_budget_or_lease_deferred_mail(field):
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    field.listen_worker(WORKER)
    workers = [field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind="update",
        subject=f"Large worker update {number}", body="x" * 9000,
        idempotency_key=f"large-worker-{number}",
    ) for number in range(8)]
    operator = _operator_reply(field)
    received = pull_worker_input(field, worker_name=WORKER, limit=100)
    encoded = (json.dumps({"ok": True, "result": received}, ensure_ascii=True,
                          indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert len(encoded) <= MAX_WORKER_INPUT_BYTES
    assert received["items"][0]["message"]["message_ref"] == operator["message_ref"]
    received_refs = {row["message"]["message_ref"] for row in received["items"]}
    assert 1 < len(received_refs) < len(workers) + 1
    assert received["delivery"]["limited_by"] == "response_byte_limit"
    assert received["delivery"]["remaining_count"] == len(workers) + 1 - len(received_refs)
    assert {row["message_ref"] for row in received["acquired_leases"]} == received_refs
    deferred = received["delivery"]["deferred_message_ref"]
    assert deferred not in received_refs
    assert field._mail_record_unlocked(PROJECT, WORKER, deferred)["state"] == "pending"

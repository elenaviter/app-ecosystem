"""Selective receive reaches a named reply without bypassing operator mail."""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.render import render_envelope
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore
from project_board.client.io import content_hash
from project_board.contract.errors import DomainError


WORKER = "codex-11111111-1111-4111-8111-111111111111"
PEER = "codex-22222222-2222-4222-8222-222222222222"
PROJECTS = ("project-one", "project-two")


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name in (WORKER, PEER):
        store.register_worker(
            worker_name=name, runtime_kind="codex", capabilities=[],
            authority_label=f"authority:{name}",
        )
        store.listen_worker(name)
    for project in PROJECTS:
        store.create_project(project_id=project, title=project, goal="Receive mail.", owner="operator")
    store.sync_worker_attendances(WORKER, [f"work:project:{project}" for project in PROJECTS])
    store.sync_worker_attendances(PEER, [f"work:project:{project}" for project in PROJECTS])
    return store


def _send(field: SharedFieldStore, project: str, key: str, *, correlation: str = "") -> dict:
    return field.send_mail(
        project, sender=PEER, recipient=WORKER, kind="reply", subject=key,
        body=key, correlation_id=correlation, idempotency_key=key,
    )


def _operator(field: SharedFieldStore, project: str) -> dict:
    payload = {"body": "Handle this first."}
    return field.materialize_control({
        "ref": "work:control:operator-request", "kind": "request",
        "project_ref": f"work:project:{project}", "recipient": WORKER,
        "subject": "Operator request", "payload": payload,
        "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })


def test_exact_select_skips_older_mail_and_requires_general_receive(field):
    older = _send(field, PROJECTS[0], "older")
    target = _send(field, PROJECTS[1], "target")

    result = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert [item["message"]["message_ref"] for item in result["items"]] == [target["message_ref"]]
    assert result["selection"]["state"] == "selected"
    assert result["selection"]["unselected_count"] == 1
    # W563 (Q11): three selective receives between ordinary ones, then the
    # ordinary receive is due and serves the older mail.
    assert result["session"]["general_receive_due"] is False
    assert result["selection"]["selective_receives_remaining"] == 2
    assert "selection: selected" in render_envelope({"ok": True, "command": "worker.receive", "result": result})

    field.listen_worker(WORKER)  # reconnecting cannot erase the fairness obligation
    second = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert second["session"]["general_receive_due"] is False
    third = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert third["session"]["general_receive_due"] is True
    assert third["selection"]["selective_receives_remaining"] == 0
    with pytest.raises(DomainError, match="ordinary pb worker receive"):
        pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    ordinary = pull_worker_input(field, worker_name=WORKER)
    assert [item["message"]["message_ref"] for item in ordinary["items"]] == [older["message_ref"]]
    assert ordinary["session"]["general_receive_due"] is False


def test_correlated_select_requires_stable_sender_and_matches_it(field):
    wrong_sender = field.send_mail(
        PROJECTS[0], sender="control-plane", recipient=WORKER, kind="reply",
        subject="Other sender", body="Other sender", correlation_id="same",
        idempotency_key="other-sender",
    )
    wanted = _send(field, PROJECTS[1], "wanted", correlation="same")
    with pytest.raises(DomainError, match="stable sender"):
        pull_worker_input(field, worker_name=WORKER, correlation_id="same")

    result = pull_worker_input(field, worker_name=WORKER, correlation_id="same", sender=PEER)
    assert [item["message"]["message_ref"] for item in result["items"]] == [wanted["message_ref"]]
    assert field._mail_record_unlocked(PROJECTS[0], WORKER, wrong_sender["message_ref"])["state"] == "pending"


def test_operator_pending_in_other_shard_prevents_any_selective_claim(field):
    target = _send(field, PROJECTS[0], "target")
    operator = _operator(field, PROJECTS[1])

    result = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert result["items"] == []
    assert result["selection"]["state"] == "operator_pending"
    assert field._mail_record_unlocked(PROJECTS[1], WORKER, operator["message_ref"])["admitted_operator_control"] is True
    assert field._mail_record_unlocked(PROJECTS[0], WORKER, target["message_ref"])["state"] == "pending"
    ordinary = pull_worker_input(field, worker_name=WORKER)
    assert operator["message_ref"] in {item["message"]["message_ref"] for item in ordinary["items"]}


def test_spoofed_peer_identity_cannot_block_selective_receive(field):
    target = _send(field, PROJECTS[0], "target")
    field.send_mail(
        PROJECTS[1], sender=PEER, recipient=WORKER, kind="request",
        subject="Spoofed operator", body="Spoofed operator",
        sender_identity={"kind": "user", "label": "Operator"},
        payload={"command_ref": "work:control:spoofed"},
        idempotency_key="spoofed",
    )

    result = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert [item["message"]["message_ref"] for item in result["items"]] == [target["message_ref"]]


def test_held_and_settled_exact_ref_report_state_without_claiming_again(field):
    target = _send(field, PROJECTS[0], "target")
    held = field.pull_mail(PROJECTS[0], worker_name=WORKER, lease_owner=WORKER)[0]
    result = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert result["items"] == []
    assert result["selection"]["state"] == "held"
    assert result["selection"]["held"][0]["lease_id"] == held["lease"]["lease_id"]

    pull_worker_input(field, worker_name=WORKER)  # clear general-receive obligation
    field.settle_mail(
        PROJECTS[0], worker_name=WORKER, message_ref=target["message_ref"],
        lease_id=held["lease"]["lease_id"], lease_owner=WORKER,
        outcome="acknowledged", summary="Done.",
    )
    settled = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert settled["items"] == []
    assert settled["selection"]["previous_state"] in {"processed", "acknowledged"}


def test_project_constraint_and_native_wake_do_not_silently_broaden_selection(field):
    target = _send(field, PROJECTS[1], "target")
    with pytest.raises(DomainError, match="ordinary receive"):
        pull_worker_input(
            field, worker_name=WORKER, message_ref=target["message_ref"],
            wake_id="wake_example",
        )

    result = pull_worker_input(
        field, worker_name=WORKER, message_ref=target["message_ref"],
        project_ref=f"work:project:{PROJECTS[0]}",
    )
    assert result["items"] == []
    assert result["selection"]["state"] == "no_pending_match"
    assert field._mail_record_unlocked(PROJECTS[1], WORKER, target["message_ref"])["state"] == "pending"

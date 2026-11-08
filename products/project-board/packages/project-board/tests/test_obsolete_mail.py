"""Obsolete is a disposition, never implementation completion or a second effect."""

from pathlib import Path

import pytest

from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.client.io import read_json, atomic_write_json


@pytest.fixture
def field(tmp_path: Path):
    field = SharedFieldStore(tmp_path / "field")
    field.initialize(field_id="test")
    field.register_worker(worker_name="worker-one", runtime_kind="codex",
                          capabilities=[], authority_label="test")
    field.create_project(project_id="project-one", title="Test", goal="Test", owner="operator")
    return field


def _lease(field):
    field.send_mail("project-one", sender="control-plane", recipient="worker-one",
                    kind="request", subject="Old request", body="Already reassigned.",
                    payload={"command_ref": "work:control:command_one"},
                    idempotency_key="old-request")
    [row] = field.pull_mail("project-one", worker_name="worker-one", lease_owner="session-one")
    return dict(worker_name="worker-one", message_ref=row["message_ref"],
                lease_id=row["lease"]["lease_id"], lease_owner="session-one",
                outcome="obsolete", summary="Current coordinator records reassignment.")


def test_obsolete_replays_the_original_receipt_and_one_outbox(field):
    args = _lease(field)
    first = field.settle_mail("project-one", **args)
    again = field.settle_mail("project-one", **args)
    assert first["state"] == "obsolete"
    assert again["settled_at"] == first["settled_at"]
    assert again["remote_settlement_outbox_id"] == first["remote_settlement_outbox_id"]
    pending = list(field._outbox.in_flight("pending", worker_name="worker-one"))
    assert len(pending) == 1
    assert field._outbox.read(first["remote_settlement_outbox_id"])["payload"]["outcome"] == "obsolete"
    assert field.list_worker_mail_leases("worker-one", lease_owner="session-one")["items"] == []


@pytest.mark.parametrize("change", [{"outcome": "acknowledged"}, {"summary": "changed"},
                                   {"lease_id": "another"}, {"lease_owner": "another"}])
def test_obsolete_retry_cannot_change_the_terminal_identity(field, change):
    args = _lease(field)
    first = field.settle_mail("project-one", **args)
    with pytest.raises(DomainError) as error:
        field.settle_mail("project-one", **{**args, **change})
    assert error.value.code == "field_mail_settlement_conflict"
    assert field._outbox.read(first["remote_settlement_outbox_id"])["payload"]["outcome"] == "obsolete"


def test_obsolete_recovers_history_to_outbox_interruption(field, monkeypatch):
    args = _lease(field)
    enqueue = field.enqueue_control_settlement
    def interrupted(*a, **kw):
        raise RuntimeError("interrupted after history commit")
    monkeypatch.setattr(field, "enqueue_control_settlement", interrupted)
    with pytest.raises(RuntimeError):
        field.settle_mail("project-one", **args)
    monkeypatch.setattr(field, "enqueue_control_settlement", enqueue)
    result = field.settle_mail("project-one", **args)
    replay = field.settle_mail("project-one", **args)
    assert result["settled_at"] == replay["settled_at"]
    assert len(list(field._outbox.in_flight("pending", worker_name="worker-one"))) == 1


def test_obsolete_still_requires_a_current_lease(field):
    args = _lease(field)
    with pytest.raises(DomainError) as error:
        field.settle_mail("project-one", **{**args, "lease_id": "wrong"})
    assert error.value.code == "field_mail_lease_mismatch"


def test_obsolete_does_not_turn_an_expired_lease_into_completion(field):
    args = _lease(field)
    path = field._mail_root("project-one", "worker-one") / "leased" / (args["message_ref"].split(":")[3] + ".json")
    row = read_json(path)
    row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    atomic_write_json(path, row)
    with pytest.raises(DomainError) as error:
        field.settle_mail("project-one", **args)
    assert error.value.code == "field_mail_lease_expired"
    assert list(field._outbox.in_flight("pending", worker_name="worker-one")) == []


def test_obsolete_preserves_the_operator_visible_reply_fence(field):
    args = _lease(field)
    path = field._mail_root("project-one", "worker-one") / "leased" / (args["message_ref"].split(":")[3] + ".json")
    row = read_json(path)
    row["sender_identity"] = {"kind": "user", "label": "Fixture owner"}
    atomic_write_json(path, row)
    with pytest.raises(DomainError) as error:
        field.settle_mail("project-one", **args)
    assert error.value.code == "field_operator_response_required"

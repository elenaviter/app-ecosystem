"""W616: an answer to a person's message carries its origin whatever its kind.

The board returns mail that carries the origin to the person who wrote; the
client attaches it from the exact delivered message (--reply-to) on the same
thread, for every operator-mail kind, not only ``reply``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

PROJECT = "w616-project"
WORKER = "codex-agent"
ORIGIN = "origin_" + "a" * 32
# A delivered control's thread is its own ref.
CONTROL = "work:control:20261007T180000Z:command_w616:question"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="w616-test")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[],
                          authority_label=f"authority:{WORKER}")
    store.create_project(project_id=PROJECT, title="W616", goal="Answers reach their writer.", owner="operator")
    store.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    return store


def _delivered(field: SharedFieldStore) -> dict:
    """A person's message to this worker, as the relay materializes it (with its origin)."""
    payload = {"body": "Which base?", "correlation_id": "thread-b"}
    return field.materialize_control({
        "ref": CONTROL,
        "project_ref": f"work:project:{PROJECT}", "recipient": WORKER, "kind": "request",
        "subject": "Which base?", "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Writer"},
        "operator_origin": {"ref": ORIGIN, "channel": "board"},
    })


def _answer(field, delivered, *, kind, correlation=CONTROL, key):
    queued = field.enqueue_remote_mail(PROJECT, sender=WORKER, recipient="operator", kind=kind,
                                       subject=f"Re: {kind}", body="Use B.", correlation_id=correlation,
                                       reply_to=delivered["message_ref"], idempotency_key=key)
    return field.read_outbox_record(queued["outbox_id"])["payload"]["payload"]


@pytest.mark.parametrize("kind", ["reply", "update", "progress", "decision", "question"])
def test_every_kind_answering_the_delivered_message_carries_its_origin(field, kind):
    delivered = _delivered(field)
    assert _answer(field, delivered, kind=kind, key=f"answer-{kind}") == {"operator_origin_ref": ORIGIN}


def test_an_update_on_another_thread_carries_no_origin_and_is_not_refused(field):
    delivered = _delivered(field)
    assert _answer(field, delivered, kind="update", correlation="another-thread", key="update-other") == {}


def test_a_reply_on_another_thread_is_still_refused(field):
    delivered = _delivered(field)
    with pytest.raises(DomainError) as refused:
        _answer(field, delivered, kind="reply", correlation="another-thread", key="reply-other")
    assert refused.value.code == "field_operator_origin_mismatch"


@pytest.mark.parametrize("routed", ["thread_writer", "owner_default", "owner_explicit", "something-else"])
def test_the_outbox_status_shows_whom_the_board_routed_it_to(field, routed):
    from project_board.client.render import render_envelope

    queued = field.enqueue_remote_mail(PROJECT, sender=WORKER, recipient="operator", kind="update",
                                       subject="Status", body="Done.", idempotency_key=f"status-{routed}")
    (leased,) = field.pull_outbox(relay_id="test-relay", kinds={"mail.route"})
    field.settle_outbox(leased["outbox_id"], relay_id="test-relay", outcome="sent", remote_disposition="accepted",
                        remote_result={"disposition": "accepted", "routed_to": routed})
    status = field.worker_outbox_status(worker_name=WORKER, outbox_id=queued["outbox_id"])
    if routed == "something-else":
        assert "routed_to" not in status  # only the board's known facts are projected
    else:
        assert status["routed_to"] == routed
        assert routed in render_envelope({"ok": True, "result": status})

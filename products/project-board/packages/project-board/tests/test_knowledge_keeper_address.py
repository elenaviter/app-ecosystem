"""`knowledge-keeper` is a role address the board resolves at send time (W517).

Like `coordinator`, the client never resolves it locally, even when the
holder is a session on this host: only the board knows who holds the role
when the mail is sent. The relay keeps the role as the board reports it, and
a heartbeat answered earlier never replaces a later revision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.render import _render_worker_context
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.contract.operator_mail_contract import (
    KNOWLEDGE_KEEPER_RECIPIENT,
    ROLE_RECIPIENTS,
)

WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    # A local worker that happens to hold the role today: still routed to the board.
    store.register_worker(worker_name="claude-code-keeper", runtime_kind="claude-code", capabilities=[], authority_label="authority:keeper")
    store.create_project(project_id=PROJECT, title="Keeper", goal="Address the role.", owner="operator")
    return store


def test_the_keeper_address_always_goes_to_the_board(field):
    assert KNOWLEDGE_KEEPER_RECIPIENT == "knowledge-keeper"
    assert {"coordinator", "knowledge-keeper"} == set(ROLE_RECIPIENTS)
    assert field.resolve_mail_recipient(PROJECT, "knowledge-keeper") == {
        "worker_name": "knowledge-keeper",
        "route": "remote",
        "pool_status": "active",
    }
    assert field.resolve_mail_recipient(PROJECT, "Knowledge-Keeper")["route"] == "remote"


def test_mail_to_the_keeper_is_queued_for_the_board_with_the_role_as_recipient(field):
    queued = field.enqueue_remote_mail(
        PROJECT, sender=WORKER, recipient="knowledge-keeper", kind="update",
        subject="Hand-over: merged", body="One line.", idempotency_key="to-the-keeper-1",
    )
    [claimed] = field.pull_outbox(relay_id="relay-01", kinds={"mail.route"})
    assert claimed["outbox_id"] == queued["outbox_id"]
    assert claimed["payload"]["recipient"] == "knowledge-keeper"


def test_the_keeper_address_needs_the_project_whose_role_it_is(field):
    with pytest.raises(DomainError) as refused:
        field.resolve_mail_recipient("", "knowledge-keeper")
    assert refused.value.code == "field_project_context_required"
    assert refused.value.details["argument"] == "--project-ref"


def test_the_relay_keeps_the_newest_role_revision(field):
    newer = {
        "knowledge-keeper": {
            "state": "held", "address": "knowledge-keeper", "revision": 3,
            "holder": {"worker_name": "claude-code-keeper", "worker_alias": "keeper"},
            "available": True, "unavailable_reason": "",
            "pending_handovers": {"count": 2, "oldest_at": "2026-10-04T08:00:00Z", "overdue": False, "items": []},
        }
    }
    older = {"knowledge-keeper": {**newer["knowledge-keeper"], "revision": 2, "holder": None, "state": "declared_unassigned"}}
    field.sync_project_roles(PROJECT, newer)
    assert field.sync_project_roles(PROJECT, older) is not None
    kept = field.read_project_roles(PROJECT)["knowledge-keeper"]
    assert kept["revision"] == 3
    assert kept["holder"]["worker_name"] == "claude-code-keeper"
    assert kept["pending_handovers"] == {"count": 2, "oldest_at": "2026-10-04T08:00:00Z", "overdue": False}


def test_brief_context_shows_the_role_beside_the_coordinator():
    text = "\n".join(
        _render_worker_context(
            {
                "roles": {
                    "knowledge-keeper": {
                        "state": "held_unavailable", "revision": 3,
                        "holder": {"worker_name": "claude-code-keeper"},
                        "unavailable_reason": "rate_limited",
                        "pending_handovers": {"count": 2, "oldest_at": "2026-10-03T08:00:00Z", "overdue": True},
                    }
                },
            }
        )
    )
    assert (
        "role knowledge-keeper: state held_unavailable · holder claude-code-keeper · revision 3 · pending hand-overs 2 · oldest since 2026-10-03T08:00:00Z (overdue)"
        in text
    )
    assert "role knowledge-keeper.unavailable_reason = rate_limited" in text


def test_forwarding_to_the_keeper_without_a_project_is_refused_locally(field):
    # Review P2: the forward path treats every role address like coordinator.
    sent = field.send_mail(
        "", sender="control-plane", recipient=WORKER, kind="request",
        subject="Direct", body="Forward me.", idempotency_key="direct-forward",
    )
    [leased] = field.pull_mail("", worker_name=WORKER, lease_owner="session-api")
    with pytest.raises(DomainError) as refused:
        field.forward_worker_mail(
            "", worker_name=WORKER, message_ref=sent["message_ref"],
            lease_id=leased["lease"]["lease_id"], lease_owner="session-api",
            recipient="knowledge-keeper", idempotency_key="forward-to-keeper",
        )
    assert refused.value.code == "field_project_context_required"
    assert not field.pull_outbox(relay_id="relay-01", kinds={"mail.route"})

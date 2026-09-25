"""`coordinator` is an address for the role, resolved by the board at send time (W313 step 5).

Workers and procedure text address the acting coordinator, not the person
holding the role today: during a hand-over the holder changes and a stable
name would reach the wrong agent. The client never resolves the role locally,
even when the holder is a session on this host, because only the board knows
who holds it now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.contract.operator_mail_contract import COORDINATOR_RECIPIENT

WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    # A local worker that happens to be the holder today: still routed to the board.
    store.register_worker(worker_name="claude-code-holder", runtime_kind="claude-code", capabilities=[], authority_label="authority:holder")
    store.create_project(project_id=PROJECT, title="Handover", goal="Address the role.", owner="operator")
    return store


def test_the_role_address_always_goes_to_the_board(field):
    assert COORDINATOR_RECIPIENT == "coordinator"
    assert field.resolve_mail_recipient(PROJECT, "coordinator") == {
        "worker_name": "coordinator",
        "route": "remote",
        "pool_status": "active",
    }
    assert field.resolve_mail_recipient(PROJECT, "Coordinator")["route"] == "remote"


def test_mail_to_the_coordinator_is_queued_for_the_board_with_the_role_as_recipient(field):
    queued = field.enqueue_remote_mail(
        PROJECT, sender=WORKER, recipient="coordinator", kind="question",
        subject="Who merges #106?", body="One line.", idempotency_key="to-the-role-1",
    )
    [claimed] = field.pull_outbox(relay_id="relay-01", kinds={"mail.route"})
    assert claimed["outbox_id"] == queued["outbox_id"]
    assert claimed["payload"]["recipient"] == "coordinator"


def test_the_role_needs_the_project_whose_coordinator_it_is(field):
    with pytest.raises(DomainError) as refused:
        field.resolve_mail_recipient("", "coordinator")
    assert refused.value.code == "field_project_context_required"
    assert refused.value.details["argument"] == "--project-ref"

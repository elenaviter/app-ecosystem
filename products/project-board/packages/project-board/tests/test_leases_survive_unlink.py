"""An agent unlinked mid-task can still settle what it holds (W325).

Found by codex-coord, 2026-09-25 about 14:14Z: it had received the welcome,
done the work and replied, and before it settled, the operator removed its
attendance. The host's mailbox sweep then archived the leased message too,
so `pb worker leases` no longer listed it and `pb worker settle` returned
field_record_not_found. Held leases now stay until they settle or expire;
only unleased mail is archived, and its sender is told.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
COORD_SESSION = "11111111-1111-4111-8111-111111111111"
MAIN_SESSION = "22222222-2222-4222-8222-222222222222"
WORKER = f"codex-{COORD_SESSION}"
SENDER = f"codex-{MAIN_SESSION}"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name, session in ((WORKER, COORD_SESSION), (SENDER, MAIN_SESSION)):
        store.register_worker(
            worker_name=name, runtime_kind="codex", runtime_session_id=session,
            capabilities=[], authority_label=f"authority:{session}",
        )
        store.listen_worker(name)
    store.create_project(project_id=PROJECT, title="Leave", goal="Settle what you hold.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    store.sync_worker_attendances(SENDER, [PROJECT_REF])
    return store


def _send(field, number):
    return field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind="request",
        subject=f"Task {number}", body="Do it.", idempotency_key=f"task-{number}",
    )


def test_a_lease_held_across_an_unlink_is_listed_read_and_settled(field):
    _send(field, 1)
    received = pull_worker_input(field, worker_name=WORKER, limit=1, lease_seconds=600)
    [item] = received["items"]
    message_ref = item["message"]["message_ref"]
    lease_id = item["message"]["lease"]["lease_id"]
    owner = item["message"]["lease"]["owner"]
    _send(field, 2)  # arrives but is never leased

    # The operator removes the attendance; the relay observes it and sweeps.
    field.sync_worker_attendances(WORKER, [])
    swept = field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=SENDER)

    listed = field.list_worker_mail_leases(WORKER, lease_owner=owner)
    assert [lease["message_ref"] for lease in listed["items"]] == [message_ref]
    reread = field.read_worker_mail_lease(
        PROJECT, worker_name=WORKER, message_ref=message_ref, lease_id=lease_id, lease_owner=owner,
    )
    assert reread["message_ref"] == message_ref
    assert reread["lease"]["lease_id"] == lease_id
    settled = field.settle_mail(
        PROJECT, worker_name=WORKER, message_ref=message_ref, lease_id=lease_id,
        lease_owner=owner, outcome="acknowledged", summary="Done before the unlink.",
    )
    assert settled["delivery_status"] == "acknowledged"

    # The unleased message is archived as undeliverable, and its sender told.
    [archived] = [row for row in swept["archived_mailboxes"] if row["recipient"] == WORKER]
    assert archived["disposition"] == "recipient_not_linked"
    assert archived["count"] == 1
    assert swept["archived_count"] == 1
    assert swept["failure_notices"] == 1

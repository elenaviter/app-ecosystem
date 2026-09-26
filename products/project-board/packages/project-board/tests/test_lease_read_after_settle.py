"""`pb worker lease-read` on a settled lease says it was settled (W339).

Settled mail moved from the mailbox's processed/ folder into partitioned
history in 87418a9, and the lease reader kept looking in the folder. A
settled lease then read back as `field_mail_lease_not_found`, which tells a
worker nothing about whether to handle the message again. Found by the
applications suite (test_lease_reader_distinguishes_expired_settled_and_absent_mail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name in ("codex-api", "claude-docs"):
        store.register_worker(
            worker_name=name,
            runtime_kind="codex",
            capabilities=[],
            authority_label=f"authority:{name}",
        )
    store.create_project(
        project_id=PROJECT, title="Lease read", goal="Read a settled lease.", owner="operator"
    )
    return store


def test_a_settled_lease_reads_back_as_already_settled(field):
    sent = field.send_mail(
        PROJECT,
        sender="codex-api",
        recipient="claude-docs",
        kind="request",
        subject="Settle me",
        body="Then read the lease again.",
        idempotency_key="settle-then-read",
    )
    [leased] = field.pull_mail(PROJECT, worker_name="claude-docs", lease_owner="session-docs")
    lease_id = leased["lease"]["lease_id"]
    field.settle_mail(
        PROJECT,
        worker_name="claude-docs",
        message_ref=sent["message_ref"],
        lease_id=lease_id,
        lease_owner="session-docs",
        outcome="acknowledged",
    )

    with pytest.raises(DomainError) as settled:
        field.read_worker_mail_lease(
            PROJECT,
            worker_name="claude-docs",
            message_ref=sent["message_ref"],
            lease_id=lease_id,
            lease_owner="session-docs",
        )

    assert settled.value.code == "field_mail_already_settled"
    assert settled.value.details["mailbox_state"] == "processed"

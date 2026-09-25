from __future__ import annotations

import json
from pathlib import Path

from project_board.client.history_migration import migrate_flat_history
from project_board.client.journal_receipt_store import JournalReceiptStore
from project_board.client.keyed_history import KeyedHistoryStore
from project_board.client.mail_history import MailHistoryStore
from project_board.client.operation_ledger import LocalOperationLedger
from project_board.client.scope_lease_store import ScopeLeaseStore


def test_flat_history_migration_is_bounded_resumable_and_keeps_bad_input(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "one.json").write_text(
        json.dumps({"worker_name": "Codex-UI", "created_at": "2026-09-25T01:00:00Z"})
    )
    (legacy / "two.json").write_text(
        json.dumps({"worker_name": "Claude-Main", "created_at": "2026-09-25T02:00:00Z"})
    )
    (legacy / "broken.json").write_text("{not json")
    history = KeyedHistoryStore(
        tmp_path / "history",
        store="history",
        retention_days=30,
    )

    first = migrate_flat_history(
        history=history,
        legacy=legacy,
        agent_for=lambda row, _path: str(row["worker_name"]),
        batch_size=1,
    )
    assert first["state"] == "running"

    second = migrate_flat_history(
        history=history,
        legacy=legacy,
        agent_for=lambda row, _path: str(row["worker_name"]),
        batch_size=10,
    )

    assert second["state"] == "complete"
    assert history.read(agent="codex-ui", record_id="one") is not None
    assert history.read(agent="claude-main", record_id="two") is not None
    assert (legacy / ".legacy-unreadable" / "broken.json").is_file()
    assert json.loads(
        (tmp_path / "history" / "codex-ui" / ".migration.json").read_text()
    )["state"] == "complete"


def test_scope_leases_keep_active_rows_pending_and_partition_terminal_rows(tmp_path: Path):
    store = ScopeLeaseStore(tmp_path / "scope-leases")
    row = {
        "lease_id": "lease_one",
        "worker_name": "Codex-UI",
        "state": "active",
        "leased_at": "2026-09-25T01:00:00Z",
        "expires_at": "2026-09-25T02:00:00Z",
    }

    active_path = store.write_active(row)
    assert active_path == tmp_path / "scope-leases" / "codex-ui" / "pending" / "lease_one.json"
    found_path, found = store.read_active("codex-ui", "lease_one") or (None, None)
    assert found and found["state"] == "active"

    terminal = {**row, "state": "released", "settled_at": "2026-09-25T01:30:00Z"}
    target = store.settle(found_path, terminal)  # type: ignore[arg-type]
    assert not active_path.exists()
    assert target.relative_to(tmp_path / "scope-leases" / "codex-ui").parts[:4] == (
        "2026",
        "09",
        "25",
        "01",
    )


def test_operation_status_by_step_uses_an_exact_pointer_and_completed_history(tmp_path: Path):
    ledger = LocalOperationLedger(tmp_path / "operations")
    record = ledger.ensure(
        kind="journal-index",
        request_hash="a" * 64,
        request={"worker_name": "codex-ui"},
        steps=["plan_validation"],
    )
    operation_id = str(record["operation_id"])
    ledger.begin_step(
        operation_id,
        "plan_validation",
        intent={"outbox_id": "outbox_one"},
    )

    found = ledger.find_by_step_value("plan_validation", "outbox_id", "outbox_one")
    assert found and found["operation_id"] == operation_id

    completed = ledger.complete_step(
        operation_id,
        "plan_validation",
        result={"outbox_id": "outbox_one"},
    )
    assert completed["state"] == "completed"
    assert not (tmp_path / "operations" / "-" / "pending" / f"{operation_id}.json").exists()
    assert ledger.read(operation_id)["state"] == "completed"


def test_journal_receipts_and_undeliverable_mail_move_out_of_pending(tmp_path: Path):
    control = tmp_path / "control"
    journal = JournalReceiptStore(control)
    receipt = {
        "entry_id": "journal_one",
        "worker_name": "codex-ui",
        "work_ref": "work:item:one",
        "created_at": "2026-09-25T03:00:00Z",
    }
    journal.write("project-one", "journal_one", receipt)
    assert journal.read("project-one", "journal_one") == receipt
    assert journal.list("project-one", work_ref="work:item:one") == [receipt]

    mail = MailHistoryStore(control)
    message = {
        "message_id": "mail_one",
        "recipient": "codex-ui",
        "state": "recipient_not_found",
        "created_at": "2026-09-25T04:00:00Z",
    }
    pending = mail.write_pending(
        project_id="project-one",
        family="mail-undeliverable",
        agent="codex-ui",
        record_id="mail_one",
        row=message,
    )
    assert len(mail.pending(project_id="project-one", family="mail-undeliverable")) == 1
    mail.settle_pending(
        project_id="project-one",
        family="mail-undeliverable",
        agent="codex-ui",
        record_id="mail_one",
        row={**message, "failure_notice_state": "delivered"},
        source=pending,
        slug="recipient-not-found",
    )
    assert mail.pending(project_id="project-one", family="mail-undeliverable") == []
    assert mail.read(
        project_id="project-one",
        family="mail-undeliverable",
        agent="codex-ui",
        record_id="mail_one",
    )["failure_notice_state"] == "delivered"

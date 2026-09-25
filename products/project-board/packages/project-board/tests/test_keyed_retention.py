"""Stores read only by key get a bound (W287 2c, rule LS2).

Handled markers, idempotency records, processed mail, operator responses,
settled scope leases, journal receipts and journal-index operations are read
by a message id, a content hash or a lease id, never listed, so they cost no
cycle time. Until W287 nothing ever removed them.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client.local_store import last_read_summaries
from project_board.client.store import SharedFieldStore


PROJECT = "project-one"
WORKER = "codex-api"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id=PROJECT, title="Bounds", goal="Every store has a bound.", owner="operator")
    return store


def _write(path: Path, *, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": path.stem}))
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_each_keyed_store_drops_records_past_its_window_and_keeps_the_rest(field):
    control = field.control
    project = field._project_dir(PROJECT)
    old = [
        _write(control / "workers" / WORKER / "handled" / "mail_old.json", age_days=31),
        _write(control / "workers" / WORKER / "idempotency" / "mail" / "old.json", age_days=31),
        _write(project / "idempotency" / "mail" / "old.json", age_days=31),
        _write(project / "idempotency" / "assignment-report" / "old.json", age_days=31),
        _write(project / "mail" / WORKER / "processed" / "mail_old.json", age_days=31),
        _write(project / "scope-leases" / "settled" / "lease_old.json", age_days=31),
        _write(control / "operator-responses" / WORKER / "old.json", age_days=31),
        _write(project / "journals" / "journal_old.json", age_days=91),
    ]
    kept = [
        _write(control / "workers" / WORKER / "handled" / "mail_new.json", age_days=1),
        _write(project / "mail" / WORKER / "processed" / "mail_new.json", age_days=29),
        # Journal receipts keep 90 days.
        _write(project / "journals" / "journal_recent.json", age_days=60),
        # A mailbox in flight is never a keyed store.
        _write(project / "mail" / WORKER / "inbox" / "mail_waiting.json", age_days=200),
    ]

    removed = maintenance.expire_keyed_stores(field, now=datetime.now(timezone.utc))

    assert [path for path in old if path.exists()] == []
    assert all(path.exists() for path in kept)
    assert removed["handled"] == 1 and removed["mail-processed"] == 1 and removed["journal-receipts"] == 1


def test_a_journal_index_lock_goes_only_after_its_operation(field):
    operations = field.control / "operations" / "journal-index"
    running = _write(operations / "journal-index_running.json", age_days=40)
    locks = [
        _write(operations / "locks" / "journal-index_running.lock", age_days=40),
        _write(operations / "locks" / "journal-index_running.execute.lock", age_days=40),
    ]
    os.utime(running, None)  # a live operation: recent
    orphans = [
        _write(operations / "locks" / "journal-index_gone.lock", age_days=40),
        _write(operations / "locks" / "journal-index_gone.execute.lock", age_days=40),
    ]

    maintenance.expire_keyed_stores(field, now=datetime.now(timezone.utc))

    assert all(lock.exists() for lock in locks)
    assert not any(lock.exists() for lock in orphans)


def test_reconciliation_counts_only_the_states_in_flight_of_a_live_mailbox(field, monkeypatch):
    project = field._project_dir(PROJECT)
    for index in range(3):
        _write(project / "mail" / WORKER / "processed" / f"mail_{index}.json", age_days=1)
    field.sync_project_mail_recipients(PROJECT, [{"worker_name": WORKER, "pool_status": "active"}])
    listed: list[str] = []
    original = Path.glob

    def watching(self, pattern):
        listed.append(self.name)
        return original(self, pattern)

    monkeypatch.setattr(Path, "glob", watching)
    field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)

    assert "processed" not in listed and "quarantine" not in listed


def test_detached_sessions_expire_but_active_sessions_are_kept(field):
    field.attach_session(PROJECT, worker_name=WORKER, session_id="old-session")
    field.detach_session(PROJECT, worker_name=WORKER, session_id="old-session")
    old = field._session_path(PROJECT, WORKER, "old-session")
    row = json.loads(old.read_text())
    row["detached_at"] = "2026-01-01T00:00:00Z"
    old.write_text(json.dumps(row))
    field.attach_session(PROJECT, worker_name=WORKER, session_id="live-session")

    removed = maintenance.apply_session_retention(
        field,
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert removed == {"removed": 1, "warnings": 0}
    assert not old.exists()
    assert field._session_path(PROJECT, WORKER, "live-session").is_file()


def test_session_listing_reports_its_bounded_current_state_read(field):
    field.attach_session(PROJECT, worker_name=WORKER, session_id="one")
    field.attach_session(PROJECT, worker_name=WORKER, session_id="two")

    assert len(field.list_sessions(PROJECT, worker_name=WORKER)) == 2
    summary = last_read_summaries(WORKER)["sessions"]
    assert summary["op"] == "list"
    assert summary["range"] == "pending/"
    assert summary["partitions"] == 1
    assert summary["records"] == 2

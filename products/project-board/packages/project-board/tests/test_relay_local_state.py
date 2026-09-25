"""The relay's local state stays proportional to in-flight work (W287).

On 2026-09-23 dev-main held 51,855 reconciliation receipts, every one empty,
and 54,827 settled outbox rows, almost all refusals filed under ``sent/``.
Every relay restart re-read all of the receipts before its first cycle
finished, which stalled startup for about four minutes. These tests pin the
rules in ``docs/project-board/storage-and-retention.md`` ("Relay Local State")
at the places that broke them.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client import reconciliation_receipts as receipts
from project_board.client.local_store import last_read_summaries
from project_board.client.outbox_store import OutboxStore, state_of_name
from project_board.client.reconciliation_publication import OUTBOX_KIND
from project_board.client.store import SharedFieldStore
from project_board.contract.mailbox_reconciliation_contract import (
    MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
    normalize_receipt,
)
from project_board.contract.reference_records import reference_for_record


WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name, kind in ((WORKER, "codex"), ("claude-docs", "claude-code")):
        store.register_worker(worker_name=name, runtime_kind=kind, capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT, title="Local state", goal="Keep the relay's local state bounded.", owner="operator")
    return store


def _receipt(*, receipt_id: str, started_at: str = "2026-09-23T20:00:00Z", archived: int = 0) -> dict:
    value = {
        "schema": MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "project_ref": f"work:project:{PROJECT}",
        "reporter_worker_name": WORKER,
        "host_id": "host-01",
        "relay_id": "relay-01",
        "started_at": started_at,
        "completed_at": started_at.replace(":00Z", ":04Z"),
        "examined": {"recipient_directory_entries": 1, "undeliverable_records": 0, "mailboxes": []},
        "archived_count": archived,
        "archived_mailboxes": (
            [{"recipient": "retired-worker", "count": archived, "archive_ref": "local:archive:one"}]
            if archived
            else []
        ),
        "failure_notices": [],
        "report_failures": [],
    }
    value["receipt_ref"] = reference_for_record("mail_reconciliation", value)
    return normalize_receipt(value)


def _publication_rows(field: SharedFieldStore) -> dict[str, list[dict]]:
    """Publication rows by where they are: in flight per agent, or settled by state."""

    outbox = OutboxStore(field.control)
    rows: dict[str, list[dict]] = {"pending": [], "leased": [], "sent": [], "refused": []}
    for folder in ("pending", "leased"):
        for path in outbox.in_flight(folder):
            row = json.loads(path.read_text())
            if row.get("kind") == OUTBOX_KIND:
                rows[folder].append(row)
    for path in outbox.settled_paths(project_ref=f"work:project:{PROJECT}", op="test"):
        row = json.loads(path.read_text())
        state = state_of_name(path.name) or str(row.get("state") or "")
        if row.get("kind") == OUTBOX_KIND:
            rows["refused" if state == "refused" else "sent"].append(row)
    return rows


def _settle(field: SharedFieldStore, outcome: str) -> None:
    for row in field.pull_outbox(relay_id="relay-01", kinds={OUTBOX_KIND}):
        field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome=outcome, remote_disposition=outcome)


def test_a_run_that_changes_nothing_writes_no_receipt_only_the_marker(field):
    first = field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)
    second = field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)

    assert first["receipt_ref"] == "" and first["receipt_stored"] is False
    assert second["archived_count"] == 0
    agent = receipts.agent_root(field, PROJECT, WORKER)
    assert sorted(p.name for p in agent.iterdir()) == ["marker.json"]
    assert not (field._project_dir(PROJECT) / "mail" / "reconciliation-receipts").exists()
    assert _publication_rows(field) == {"pending": [], "leased": [], "sent": [], "refused": []}
    marker = receipts.read_marker(field, PROJECT, WORKER)
    assert marker["runs_total"] == 2 and marker["runs_since_receipt"] == 2
    assert marker["last_run_examined"]["recipient_directory_entries"] >= 0


def test_a_run_that_archived_mail_keeps_a_receipt_until_it_is_published(field):
    receipt = _receipt(receipt_id="mailbox-reconciliation_20260923T200000Z_a1b2", archived=2)

    stored = receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=receipt)

    assert stored["stored"] is True
    pending = receipts.pending_path(field, PROJECT, WORKER, receipt["receipt_id"])
    assert pending.is_file()
    assert len(_publication_rows(field)["pending"]) == 1
    assert receipts.read_marker(field, PROJECT, WORKER)["last_receipt_ref"] == receipt["receipt_ref"]

    _settle(field, "sent")
    recovered = receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)

    assert recovered["receipts_settled"] == 1
    assert not pending.exists()
    finished = receipts.partition_path(field, PROJECT, WORKER, receipt, publication="published")
    assert finished.is_file()
    assert finished.parent.relative_to(receipts.agent_root(field, PROJECT, WORKER)).parts == ("2026", "09", "23", "20")
    assert finished.name.startswith("20260923T200000Z_20260923T200004Z_published_")


def test_empty_receipt_recovery_reports_the_pending_read(field):
    recovered = receipts.recover_unpublished_receipts(
        field,
        PROJECT,
        worker_name=WORKER,
    )

    assert recovered == {
        "receipts_recovered": 0,
        "publication_batches": 0,
        "receipts_settled": 0,
    }
    read = last_read_summaries(WORKER)[receipts.STORE]
    assert read["store"] == receipts.STORE
    assert read["op"] == "startup_recovery"
    assert read["range"] == "pending/"
    assert read["partitions"] == 1 and read["records"] == 0


def test_a_refused_publication_is_filed_as_refused_and_retention_keeps_it(field):
    refused = _receipt(receipt_id="mailbox-reconciliation_20260801T100000Z_c3d4", started_at="2026-08-01T10:00:00Z", archived=1)
    receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=refused)
    _settle(field, "refused")

    rows = _publication_rows(field)
    assert len(rows["refused"]) == 1 and rows["sent"] == []
    assert rows["refused"][0]["state"] == "refused"

    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)
    kept = receipts.partition_path(field, PROJECT, WORKER, refused, publication="refused")
    assert kept.is_file()

    published = _receipt(receipt_id="mailbox-reconciliation_20260801T110000Z_e5f6", started_at="2026-08-01T11:00:00Z", archived=1)
    receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=published)
    _settle(field, "sent")
    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)
    removable = receipts.partition_path(field, PROJECT, WORKER, published, publication="published")
    assert removable.is_file()

    result = receipts.apply_receipt_retention(field, PROJECT, now=datetime(2026, 9, 24, tzinfo=timezone.utc))

    assert result == {
        "partitions_removed": 1,
        "receipts_removed": 1,
        "receipts_kept_refused": 1,
        "size_bound_warnings": 0,
    }
    assert kept.is_file() and not removable.exists()
    read = last_read_summaries(WORKER)[receipts.STORE]
    assert read["op"] == "retention"
    assert read["range"] == "2026-08-01T10..2026-08-01T11"
    assert read["partitions"] == 2 and read["records"] == 2


def test_receipt_retention_decides_from_names_without_opening_a_receipt(field, monkeypatch):
    receipt = _receipt(receipt_id="mailbox-reconciliation_20260801T100000Z_0a0b", started_at="2026-08-01T10:00:00Z", archived=1)
    receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=receipt)
    _settle(field, "sent")
    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)

    def no_reads(*_args, **_kwargs):
        raise AssertionError("retention must decide from folder and file names")

    monkeypatch.setattr(receipts, "read_json", no_reads)
    assert receipts.apply_receipt_retention(field, PROJECT, now=datetime(2026, 9, 24, tzinfo=timezone.utc))["receipts_removed"] == 1


def test_published_receipts_are_bounded_by_count(field, monkeypatch):
    stored: list[Path] = []
    for hour in (10, 11):
        receipt = _receipt(
            receipt_id=f"mailbox-reconciliation_20260923T{hour:02d}0000Z_b{hour}",
            started_at=f"2026-09-23T{hour:02d}:00:00Z",
            archived=1,
        )
        path = receipts.partition_path(
            field,
            PROJECT,
            WORKER,
            receipt,
            publication="published",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"receipt": receipt}), encoding="utf-8")
        stored.append(path)
    monkeypatch.setattr(receipts, "MAX_RECORDS_PER_AGENT", 1)
    monkeypatch.setattr(receipts, "MAX_BYTES_PER_AGENT", 1024 * 1024)

    result = receipts.apply_receipt_retention(
        field,
        PROJECT,
        now=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )

    assert result["partitions_removed"] == 1
    assert result["receipts_removed"] == 1
    assert not stored[0].exists() and stored[1].is_file()


def test_legacy_undeliverable_replay_preserves_newer_pending_state(field):
    newer = {
        "message_id": "mail_crash_replay",
        "recipient": WORKER,
        "state": "recipient_not_found",
        "created_at": "2026-09-24T23:00:00Z",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "failure_notice_state": "reporting",
        "migration_generation": "newer-pending",
    }
    pending = field._mail_history().write_pending(
        project_id=PROJECT,
        family="mail-undeliverable",
        agent=WORKER,
        record_id="mail_crash_replay",
        row=newer,
    )
    legacy = (
        field._project_dir(PROJECT)
        / "mail"
        / "undeliverable"
        / WORKER
        / "mail_crash_replay.json"
    )
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                **newer,
                "failure_notice_state": "",
                "migration_generation": "older-legacy",
            }
        ),
        encoding="utf-8",
    )

    field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)

    assert not legacy.exists()
    preserved = json.loads(pending.read_text(encoding="utf-8"))
    assert preserved["migration_generation"] == "newer-pending"
    assert preserved["failure_notice_state"] == "reporting"


def test_settled_outbox_rows_expire_and_rows_in_flight_never_do(field):
    root = field.control / "outbox"
    old = time.time() - 40 * 86400
    for folder, name in (("sent", "old-sent"), ("refused", "old-refused"), ("sent", "fresh-sent"), ("pending", "old-pending")):
        (root / folder).mkdir(parents=True, exist_ok=True)
        path = root / folder / f"{name}.json"
        path.write_text(json.dumps({"outbox_id": name, "state": folder}))
        if name.startswith("old"):
            os.utime(path, (old, old))

    removed = maintenance.apply_outbox_retention(field)

    # Rows from before 2b in the flat folders expire by modification time.
    assert removed["flat"] == 2
    assert sorted(p.name for p in root.rglob("*.json")) == ["fresh-sent.json", "old-pending.json"]


def _legacy_receipt(field, receipt, *, outbox_ids, folder="refused"):
    legacy = field._project_dir(PROJECT) / "mail" / "reconciliation-receipts"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / f"{receipt['receipt_id']}.json").write_text(json.dumps({
        "schema": receipts.LOCAL_RECEIPT_RECORD_SCHEMA,
        "receipt": receipt,
        "publication": {"kind": OUTBOX_KIND, "outbox_ids": outbox_ids, "batch_count": len(outbox_ids)},
    }))
    for outbox_id in outbox_ids:
        (field.control / "outbox" / folder).mkdir(parents=True, exist_ok=True)
        (field.control / "outbox" / folder / f"{outbox_id}.json").write_text(json.dumps({
            "outbox_id": outbox_id, "kind": OUTBOX_KIND, "state": folder if folder != "leased" else "leased",
        }))
    return legacy


def test_legacy_cleanup_deletes_empty_receipts_moves_the_rest_and_waits_for_leased_rows(field):
    for index in range(3):
        _legacy_receipt(field, _receipt(receipt_id=f"mailbox-reconciliation_20260916T21000{index}Z_000{index}"), outbox_ids=[f"outbox_mailrecon_empty{index}"])
    busy = _receipt(receipt_id="mailbox-reconciliation_20260916T220000Z_beef")
    _legacy_receipt(field, busy, outbox_ids=["outbox_mailrecon_busy"], folder="leased")
    informative = _receipt(receipt_id="mailbox-reconciliation_20260916T230000Z_cafe", started_at="2026-09-16T23:00:00Z", archived=1)
    legacy = _legacy_receipt(field, informative, outbox_ids=[])

    first = maintenance.cleanup_legacy_receipts(field, PROJECT)

    assert first["state"] == "running" and first["claimed_remaining"] == 1
    assert {k: v for k, v in first["agents"][WORKER].items() if v} == {"deleted": 3, "moved": 1, "outbox_rows_deleted": 3, "deferred": 1}
    assert not legacy.exists()
    assert receipts.pending_path(field, PROJECT, WORKER, informative["receipt_id"]).is_file()
    assert (field.control / "outbox" / "leased" / "outbox_mailrecon_busy.json").is_file()

    # The delivery in flight finishes; the next pass completes the cleanup.
    os.replace(field.control / "outbox" / "leased" / "outbox_mailrecon_busy.json", field.control / "outbox" / "refused" / "outbox_mailrecon_busy.json")
    second = maintenance.cleanup_legacy_receipts(field, PROJECT)

    assert second["state"] == "complete"
    progress = json.loads((receipts.store_root(field, PROJECT) / ".legacy-cleanup.json").read_text())
    assert progress["agents"][WORKER]["deleted"] == 4 and progress["agents"][WORKER]["moved"] == 1
    assert maintenance.cleanup_legacy_receipts(field, PROJECT) == {"state": "complete"}


def test_retention_runs_once_an_hour_by_a_fact_kept_in_the_field(field):
    now = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)
    assert maintenance.run_local_state_maintenance(field, now=now)["retention"] is not None
    # A new process reads the same field: nothing in memory decides (LS4).
    again = SharedFieldStore(field.root)
    assert maintenance.run_local_state_maintenance(again, now=now + timedelta(minutes=30))["retention"] is None
    assert maintenance.run_local_state_maintenance(again, now=now + timedelta(minutes=61))["retention"] is not None


def test_guard_a_large_history_costs_the_cycle_nothing(field, monkeypatch):
    """Acceptance 8: 50,000 receipts and 50,000 settled rows, and the cycle reads none."""

    legacy = field._project_dir(PROJECT) / "mail" / "reconciliation-receipts"
    legacy.mkdir(parents=True)
    sent = field.control / "outbox" / "sent"
    body = json.dumps({"schema": receipts.LOCAL_RECEIPT_RECORD_SCHEMA, "receipt": {}, "publication": {}})
    row = json.dumps({"kind": OUTBOX_KIND, "state": "refused", "worker_name": WORKER, "created_at": "2026-09-20T00:00:00Z"})
    for index in range(50_000):
        (legacy / f"mailbox-reconciliation_20260920T000000Z_{index:05d}.json").write_text(body)
        (sent / f"outbox_mailrecon_{index:05d}.json").write_text(row)

    from project_board.client import io as field_io
    from project_board.client import reconciliation_publication as publication
    from project_board.client import store as store_module

    history_reads: list[Path] = []
    for module in (receipts, publication, store_module, field_io):
        original = module.read_json

        def counted(path, *args, _original=original, **kwargs):
            if Path(path).parent in {legacy, sent}:
                history_reads.append(Path(path))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(module, "read_json", counted)

    started = time.monotonic()
    field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)
    field.reconcile_project_mailboxes(PROJECT, reporter_worker_name=WORKER)
    activity = field._assignee_last_activity(PROJECT)
    elapsed = time.monotonic() - started

    assert history_reads == []
    assert WORKER not in activity or activity[WORKER]
    assert elapsed < 2.0, f"two cycles and one activity read took {elapsed:.2f}s over a 50,000-record history"

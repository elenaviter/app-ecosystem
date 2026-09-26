"""Refused reconciliation receipts are published again once the grant is proven (W287 item 8)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from project_board.client import local_state_maintenance as maintenance
from project_board.client import reconciliation_receipts as receipts
from project_board.client import reconciliation_replay as replay
from project_board.client.reconciliation_publication import OUTBOX_KIND

from test_relay_local_state import PROJECT, WORKER, _receipt, field  # noqa: F401

T0 = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)


def _settle(field, outcome: str) -> list[str]:
    settled = []
    for row in field.pull_outbox(relay_id="relay-01", kinds={OUTBOX_KIND}):
        field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome=outcome, remote_disposition=outcome)
        settled.append(row["outbox_id"])
    return settled


def _where(field, receipt_id: str) -> str:
    agent = receipts.agent_root(field, PROJECT, WORKER)
    if (agent / "pending" / f"{receipt_id}.json").is_file():
        return "pending"
    [path] = list(agent.rglob(f"*_{receipt_id}.json"))
    return path.name.split("_")[2]


def _two_refused(field) -> tuple[str, str]:
    older = _receipt(receipt_id="mailbox-reconciliation_20260926T010000Z_aaaa", started_at="2026-09-26T01:00:00Z", archived=1)
    newer = _receipt(receipt_id="mailbox-reconciliation_20260926T020000Z_bbbb", started_at="2026-09-26T02:00:00Z", archived=1)
    for receipt in (older, newer):
        receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=receipt)
    _settle(field, "refused")
    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)
    assert _where(field, older["receipt_id"]) == _where(field, newer["receipt_id"]) == "refused"
    return older["receipt_id"], newer["receipt_id"]


def test_a_probe_an_hour_until_the_grant_then_every_refused_receipt(field, caplog):
    older, newer = _two_refused(field)

    with caplog.at_level(logging.INFO, logger=replay.__name__):
        # The probe: the oldest refused receipt, under a new id; the other waits.
        assert replay.replay_refused_receipts(field, PROJECT, now=T0) == {"probes": 1, "replayed": 0, "waiting": 0}
        assert (_where(field, older), _where(field, newer)) == ("pending", "refused")
        record = json.loads((receipts.agent_root(field, PROJECT, WORKER) / "pending" / f"{older}.json").read_text())
        assert record["publication"]["replays"] == 1
        assert all(value.endswith("_r1") for value in record["publication"]["outbox_ids"])

        # Unsettled probe: wait, queue nothing more.
        assert replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=5))["waiting"] == 1

        # The grant is still missing: the probe is refused, quietly, and the
        # next probe waits for the interval.
        assert len(_settle(field, "refused")) == 1
        assert replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=10)) == {"probes": 0, "replayed": 0, "waiting": 0}
        assert _where(field, older) == "refused"
        assert field.pull_outbox(relay_id="relay-01", kinds={OUTBOX_KIND}) == []

        # An hour later, one more probe, under the next id.
        assert replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=61))["probes"] == 1
        record = json.loads((receipts.agent_root(field, PROJECT, WORKER) / "pending" / f"{older}.json").read_text())
        assert all(value.endswith("_r2") for value in record["publication"]["outbox_ids"])

        # The Card was re-approved: the probe is published, which proves it,
        # and every refused receipt is queued at once, without waiting an hour.
        assert len(_settle(field, "sent")) == 1
        assert replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=62))["replayed"] == 1
        assert (_where(field, older), _where(field, newer)) == ("published", "pending")
        _settle(field, "sent")
        receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)
        assert _where(field, newer) == "published"

        # Nothing refused is left: the marker goes, a later refusal probes again.
        assert replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=63)) == {"probes": 0, "replayed": 0, "waiting": 0}
        assert not (receipts.agent_root(field, PROJECT, WORKER) / replay.MARKER).exists()

    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("relay store replay")]
    assert any("op=probe" in line and "outcome=refused" in line for line in lines)
    assert any("op=batch replayed=1" in line for line in lines)
    # Quiet: no probe outcome became a board event.
    assert not [row for row in field.pull_outbox(relay_id="relay-01") if row.get("kind") == "event.publish"]


def test_a_batch_the_service_accepted_keeps_its_id(field, monkeypatch):
    from project_board.contract import mailbox_reconciliation_publication as batches
    from project_board.contract.mailbox_reconciliation_contract import normalize_receipt
    from project_board.contract.reference_records import reference_for_record

    # Two archived mailboxes, one batch each.
    monkeypatch.setattr(batches, "MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES", 1200)
    receipt = dict(_receipt(receipt_id="mailbox-reconciliation_20260926T030000Z_cccc", started_at="2026-09-26T03:00:00Z", archived=2))
    receipt["archived_mailboxes"] = [
        {"recipient": f"retired-{index}", "count": 1, "archive_ref": f"local:archive:{index}"} for index in range(2)
    ]
    receipt.pop("receipt_ref", None)
    receipt.pop("content_hash", None)
    receipt["receipt_ref"] = reference_for_record("mail_reconciliation", receipt)
    receipt = normalize_receipt(receipt)
    stored = receipts.record_receipt(field, PROJECT, worker_name=WORKER, receipt=receipt)
    [first, *rest] = stored["publication"]["outbox_ids"]
    assert rest, "the receipt must span two batches"
    rows = field.pull_outbox(relay_id="relay-01", kinds={OUTBOX_KIND})
    for row in rows:
        field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome="refused", remote_disposition="refused")
    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)
    # Pretend the service had accepted the first batch: rewrite its settled state.
    path, _state = field._outbox.find(first)
    row = json.loads(path.read_text())
    row["state"] = "sent"
    path.write_text(json.dumps(row))

    assert replay.replay_refused_receipts(field, PROJECT, now=T0)["probes"] == 1
    record = json.loads((receipts.agent_root(field, PROJECT, WORKER) / "pending" / f"{receipt['receipt_id']}.json").read_text())
    assert record["publication"]["outbox_ids"][0] == first
    assert all(value.endswith("_r1") for value in record["publication"]["outbox_ids"][1:])


def test_housekeeping_runs_the_replay_and_a_marker_survives_a_restart(field):
    older, _newer = _two_refused(field)
    summary = maintenance.run_local_state_maintenance(field, now=T0)
    assert summary["refused_receipts_replay"][PROJECT]["probes"] == 1
    _settle(field, "refused")
    # A restart keeps no memory but the marker: no second probe within the hour.
    assert maintenance.run_local_state_maintenance(field, now=T0 + timedelta(minutes=5))["refused_receipts_replay"][PROJECT]["probes"] == 0
    assert _where(field, older) == "refused"


def test_a_later_replay_never_reuses_an_id_after_queue_publication_rewrites_the_record(field):
    """Review of #175 (claude-app): queue_publication dropped ``replays``."""

    from project_board.client.reconciliation_publication import queue_publication

    older, _newer = _two_refused(field)
    replay.replay_refused_receipts(field, PROJECT, now=T0)
    pending = receipts.agent_root(field, PROJECT, WORKER) / "pending" / f"{older}.json"
    queue_publication(field, PROJECT, worker_name=WORKER, record_path=pending)
    assert json.loads(pending.read_text())["publication"]["replays"] == 1
    _settle(field, "refused")
    receipts.recover_unpublished_receipts(field, PROJECT, worker_name=WORKER)

    replay.replay_refused_receipts(field, PROJECT, now=T0 + timedelta(minutes=61))
    ids = json.loads(pending.read_text())["publication"]["outbox_ids"]
    assert ids and all(value.endswith("_r2") for value in ids)

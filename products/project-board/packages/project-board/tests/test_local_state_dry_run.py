"""The W287 dry run counts what housekeeping then does, and writes nothing."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_board.client import local_state_maintenance as maintenance

from test_relay_local_state import PROJECT, WORKER, _legacy_receipt, _receipt, field  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "local_state_dry_run.py"
spec = importlib.util.spec_from_file_location("local_state_dry_run", SCRIPT)
dry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dry)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() + f":{path.stat().st_mtime_ns}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_dry_run_counts_what_maintenance_does_and_changes_nothing(field):
    for index in range(3):
        _legacy_receipt(field, _receipt(receipt_id=f"mailbox-reconciliation_20260916T21000{index}Z_000{index}"), outbox_ids=[f"outbox_mailrecon_empty{index}"])
    informative = _receipt(receipt_id="mailbox-reconciliation_20260916T230000Z_cafe", started_at="2026-09-16T23:00:00Z", archived=1)
    _legacy_receipt(field, informative, outbox_ids=[])
    row = field.enqueue_service_event(PROJECT, worker_name=WORKER, kind="note.recorded", summary="note", source_event_ref="local:test:1")
    [claimed] = field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    field.settle_outbox(claimed["outbox_id"], relay_id="relay-01", outcome="sent")
    later = datetime.now(timezone.utc) + timedelta(days=31)

    before = _snapshot(field.root)
    report = dry.dry_run(field.root, later)
    assert _snapshot(field.root) == before, "the dry run wrote, renamed or deleted something"

    project = report["projects"][PROJECT]
    assert project["legacy_receipts"] == {"files": 4, "delete": 3, "move": 1, "outbox_rows_named": 3}
    assert report["flat_outbox"]["refused"] == {"delete_with_empty_receipt": 3}
    assert project["outbox"][WORKER]["remove"] == 1
    assert report["field_totals"]["files"] == len(before)
    summed = dry.totals(report)
    keyed = {store: values.get("remove", 0) for store, values in report["keyed_flat_stores"].items()}
    assert (summed["delete"], summed["move"], summed["delete_with_empty_receipt"]) == (3, 1, 3)
    events = sum(values.get("remove", 0) for values in project["events"].values())
    assert summed["remove"] == project["outbox"][WORKER]["remove"] + events + sum(keyed.values())

    done = maintenance.run_local_state_maintenance(field, now=later)

    agents = done["legacy_receipts"][PROJECT]["agents"][WORKER]
    assert (agents["deleted"], agents["moved"], agents["outbox_rows_deleted"]) == (3, 1, 3)
    assert done["retention"]["outbox"]["records"] == project["outbox"][WORKER]["remove"]
    assert {store: done["retention"]["keyed"].get(store, 0) for store in keyed} == keyed
    assert done["retention"]["events"][PROJECT]["records"] == events
    assert field.read_outbox_record(row["outbox_id"]) is None


def test_the_dry_run_renders_key_value_lines(field, capsys):
    assert dry.main([str(field.root), "--now", "2026-09-26T05:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "field_totals.files = " in out and "receipt_classification = relay" in out


def test_simulate_runs_maintenance_on_a_copy_and_leaves_the_field_alone(field, tmp_path):
    for index in range(3):
        _legacy_receipt(field, _receipt(receipt_id=f"mailbox-reconciliation_20260916T21000{index}Z_000{index}"), outbox_ids=[f"outbox_mailrecon_empty{index}"])
    before = _snapshot(field.root)

    result = dry.simulate(field.root, tmp_path / "sim", datetime.now(timezone.utc))

    assert _snapshot(field.root) == before, "simulate changed the live field"
    assert result["settled"] and result["passes"] >= 2
    # The total may grow (a partitioned store writes day indexes); the legacy
    # receipts leave the mail folder.
    mail = result["stores_changed"]["projects/*/mail"]
    assert mail["after"]["files"] < mail["before"]["files"]
    assert not (tmp_path / "sim" / "field" / ".problem-board" / "projects" / PROJECT / "mail" / "reconciliation-receipts").exists()


def test_simulate_refuses_a_work_dir_inside_the_field_and_keeps_the_copy_private(field, tmp_path):
    import pytest

    with pytest.raises(SystemExit):
        dry.simulate(field.root, field.root / "sim", datetime.now(timezone.utc))
    assert not (field.root / "sim").exists()

    dry.simulate(field.root, tmp_path / "sim", datetime.now(timezone.utc))
    assert ((tmp_path / "sim").stat().st_mode & 0o777) == 0o700

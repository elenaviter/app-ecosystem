"""The outbox per project and agent, settled rows in hour folders (W287 2b).

On 2026-09-23 dev-main held 54,827 settled rows in one flat ``outbox/sent``
folder, and a claim, a plan-node publish and every plan index listed or read
all of them. Rows in flight now live in their agent's ``pending/`` and
``leased/``; settled rows move to the hour they were created with the outcome
in the file name.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client import local_store
from project_board.client import store as store_module
from project_board.client.outbox_store import OutboxStore, state_of_name
from project_board.client.store import SharedFieldStore


WORKER = "codex-api"
PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id=PROJECT, title="Outbox", goal="Keep the outbox per agent.", owner="operator")
    return store


def _event(field: SharedFieldStore, index: int) -> dict:
    return field.enqueue_service_event(
        PROJECT,
        worker_name=WORKER,
        kind="note.recorded",
        summary=f"note {index}",
        source_event_ref=f"local:test:{index}",
    )


def test_a_queued_row_lives_in_its_agent_pending_folder(field):
    row = _event(field, 1)

    agent = field.control / "projects" / PROJECT / "outbox" / WORKER
    assert (agent / "pending" / f"{row['outbox_id']}.json").is_file()
    assert not list((field.control / "outbox").glob("*/*.json"))
    assert field.read_outbox_record(row["outbox_id"])["state"] == "pending"


def test_claim_retry_and_settle_move_the_row_and_name_the_outcome(field):
    first, second = _event(field, 1), _event(field, 2)

    claimed = field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    assert {row["outbox_id"] for row in claimed} == {first["outbox_id"], second["outbox_id"]}
    agent = field.control / "projects" / PROJECT / "outbox" / WORKER
    assert sorted(p.stem for p in (agent / "leased").glob("*.json")) == sorted([first["outbox_id"], second["outbox_id"]])

    retried = field.retry_outbox(first["outbox_id"], relay_id="relay-01", error_code="work_relay_transport_unavailable", error_summary="down")
    assert retried["state"] == "pending" and (agent / "pending" / f"{first['outbox_id']}.json").is_file()

    field.settle_outbox(second["outbox_id"], relay_id="relay-01", outcome="refused", remote_disposition="work_operation_not_granted")
    settled = [p for p in agent.rglob("*.json") if p.parent.name.isdigit()]
    assert len(settled) == 1
    assert settled[0].name.endswith(f"_{second['outbox_id']}__refused-event-publish.json")
    assert state_of_name(settled[0].name) == "refused"
    assert field.read_outbox_record(second["outbox_id"])["state"] == "refused"
    assert field.worker_outbox_status(worker_name=WORKER, outbox_id=second["outbox_id"])["state"] == "refused"


def test_settlement_appends_to_a_large_index_without_reading_or_rewriting_it(
    field, monkeypatch
):
    outbox = OutboxStore(field.control)
    pending_row = {
        "outbox_id": "outbox_large_index_guard",
        "kind": "event.publish",
        "worker_name": WORKER,
        "project_ref": PROJECT_REF,
        "state": "pending",
        "created_at": "2026-09-25T10:00:00Z",
    }
    source = outbox.write_pending(pending_row)
    ids = outbox.agent_root(PROJECT_REF, WORKER) / "2026" / "09" / "25" / "ids"
    ids.parent.mkdir(parents=True, exist_ok=True)
    ids.write_text("".join(f"old_{index:05d} 09\n" for index in range(50_000)))
    original_inode = ids.stat().st_ino
    original_size = ids.stat().st_size
    original_read_text = Path.read_text
    original_atomic_write_text = local_store.atomic_write_text

    def reject_index_read(path, *args, **kwargs):
        if path == ids:
            raise AssertionError("settlement read retained index history")
        return original_read_text(path, *args, **kwargs)

    def reject_index_rewrite(path, text):
        if path == ids:
            raise AssertionError("settlement rewrote retained index history")
        return original_atomic_write_text(path, text)

    monkeypatch.setattr(Path, "read_text", reject_index_read)
    monkeypatch.setattr(local_store, "atomic_write_text", reject_index_rewrite)

    target = outbox.settle(source, {**pending_row, "state": "sent"})

    assert target.is_file() and not source.exists()
    assert ids.stat().st_ino == original_inode
    assert ids.stat().st_size == original_size + len(
        "outbox_large_index_guard 10\n".encode("utf-8")
    )


def test_a_claim_never_opens_a_settled_row(field, monkeypatch):
    for index in range(3):
        _event(field, index)
    for row in field.pull_outbox(relay_id="relay-01", worker_name=WORKER):
        field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome="sent")
    fresh = _event(field, 9)
    original = store_module.read_json

    def no_settled(path, *args, **kwargs):
        if Path(path).parent.name.isdigit():
            raise AssertionError(f"a claim opened a settled row: {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_json", no_settled)
    claimed = field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    assert [row["outbox_id"] for row in claimed] == [fresh["outbox_id"]]


def test_flat_rows_from_before_2b_move_into_the_layout_with_their_lease(field):
    flat = field.control / "outbox"
    rows = {
        "pending": {"outbox_id": "outbox_flat_pending", "kind": "note.recorded", "worker_name": WORKER, "project_ref": PROJECT_REF, "state": "pending", "created_at": "2026-09-23T20:00:00Z"},
        "leased": {"outbox_id": "outbox_flat_leased", "kind": "note.recorded", "worker_name": WORKER, "project_ref": PROJECT_REF, "state": "leased", "lease": {"relay_id": "relay-01", "expires_at": "2099-01-01T00:00:00Z"}, "created_at": "2026-09-23T20:01:00Z"},
        "sent": {"outbox_id": "outbox_flat_sent", "kind": "mail.route", "worker_name": WORKER, "project_ref": PROJECT_REF, "state": "sent", "created_at": "2026-09-22T08:30:00Z"},
        "refused": {"outbox_id": "outbox_flat_refused", "kind": "mail.reconciliation.publish", "worker_name": WORKER, "project_ref": PROJECT_REF, "state": "refused", "created_at": "2026-09-21T07:00:00Z"},
    }
    for folder, row in rows.items():
        (flat / folder).mkdir(parents=True, exist_ok=True)
        (flat / folder / f"{row['outbox_id']}.json").write_text(json.dumps(row))
    # Before the move, every row is found by id in the flat folders.
    assert {field.read_outbox_record(row["outbox_id"])["state"] for row in rows.values()} == {"pending", "leased", "sent", "refused"}

    result = maintenance.migrate_flat_outbox(field)

    assert result == {"state": "complete", "moved": {WORKER: 4}, "unreadable": {}}
    assert not list(flat.glob("*/*.json"))
    agent = field.control / "projects" / PROJECT / "outbox" / WORKER
    assert json.loads((agent / "leased" / "outbox_flat_leased.json").read_text())["lease"]["relay_id"] == "relay-01"
    assert (agent / "pending" / "outbox_flat_pending.json").is_file()
    sent = list((agent / "2026" / "09" / "22" / "08").glob("*.json"))
    assert [state_of_name(p.name) for p in sent] == ["sent"]
    # The leased row still settles through the relay that holds it.
    field.settle_outbox("outbox_flat_leased", relay_id="relay-01", outcome="sent")
    assert field.read_outbox_record("outbox_flat_leased")["state"] == "sent"


def test_settled_rows_expire_by_hour_folder_per_agent(field, caplog):
    row = _event(field, 1)
    [claimed] = field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    field.settle_outbox(claimed["outbox_id"], relay_id="relay-01", outcome="sent")

    later = datetime.now(timezone.utc) + timedelta(days=31)
    with caplog.at_level("INFO"):
        removed = maintenance.apply_outbox_retention(field, now=later)

    assert removed["records"] == 1 and removed["partitions"] == 1
    assert field.read_outbox_record(row["outbox_id"]) is None
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("relay store read") and "op=retention" in r.getMessage()]
    assert any(f"worker={WORKER} store=outbox" in line for line in lines)


def test_refused_mail_is_listed_and_found_for_replay_from_the_layout(field):
    queued = field.enqueue_remote_mail(
        "", sender=WORKER, recipient="operator", kind="update",
        subject="Where is it?", body="One line.", idempotency_key="question-one",
    )
    [claimed] = field.pull_outbox(relay_id="relay-01", kinds={"mail.route"})
    # Mail outside a project lives under unscoped/, never among the projects.
    assert (field.control / "unscoped" / "outbox" / WORKER / "leased" / f"{queued['outbox_id']}.json").is_file()
    assert not (field.control / "projects" / "-").exists()
    field.settle_outbox(claimed["outbox_id"], relay_id="relay-01", outcome="refused", remote_disposition="work_mail_kind_invalid")

    listed = field.list_mail_deliveries(worker_name=WORKER, states=["refused"])

    assert [item["outbox_id"] for item in listed["items"]] == [queued["outbox_id"]]
    found = OutboxStore(field.control).find(queued["outbox_id"], worker_name=WORKER)
    assert found is not None and found[1] == "refused"


def test_unreadable_flat_rows_are_quarantined_and_the_migration_ends(field):
    """Review on 2b: a skipped row left in place made every batch the same, forever."""

    flat = field.control / "outbox" / "sent"
    flat.mkdir(parents=True, exist_ok=True)
    batch = 3
    for index in range(batch + 5):
        (flat / f"bad_{index:02d}.json").write_text("{not json" if index % 2 else json.dumps({"kind": "no-id"}))
    for index in range(2):
        (flat / f"outbox_good_{index}.json").write_text(json.dumps({
            "outbox_id": f"outbox_good_{index}", "kind": "mail.route", "worker_name": WORKER,
            "project_ref": PROJECT_REF, "state": "sent", "created_at": "2026-09-22T08:00:00Z",
        }))

    totals = {"moved": {}, "unreadable": {}}
    while True:
        result = maintenance.migrate_flat_outbox(field, batch_size=batch)
        for family in ("moved", "unreadable"):
            for agent, count in result[family].items():
                totals[family][agent] = totals[family].get(agent, 0) + count
        if result["state"] == "complete":
            break

    assert totals["moved"] == {WORKER: 2}
    assert sum(totals["unreadable"].values()) == batch + 5
    assert not list(flat.glob("*.json"))
    assert len(list((field.control / "outbox" / ".legacy-unreadable" / "sent").glob("*.json"))) == batch + 5
    assert field.read_outbox_record("outbox_good_1")["state"] == "sent"
    assert json.loads(
        (field._project_dir(PROJECT) / "outbox" / WORKER / ".migration.json").read_text()
    )["state"] == "complete"

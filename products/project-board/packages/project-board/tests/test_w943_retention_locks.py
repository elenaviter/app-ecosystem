"""W943 F2-C: retention never holds a lock writers need for a whole pass.

On 2026-10-23 dev-main's hourly retention passes took 8-24 s each. Outbox
retention held the host-wide outbox lock, and inside it the partition expire
held the store lock while it statted every settled row of every agent, so
every agent's claim, retry and settle on the host waited for the pass.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client import local_store
from project_board.client.local_store import PartitionedStore
from project_board.client.outbox_store import OutboxStore
from project_board.client.store import SharedFieldStore

SLOW_SECONDS = 0.6


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, 5, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> PartitionedStore:
    store = PartitionedStore(tmp_path / "history", store="events")
    for day in (20, 21, 22):
        for hour in (9, 10):
            store.write("codex-api", f"row_{day}_{hour}", _at(day, hour), {"day": day, "hour": hour})
    return store


def _slow_inventory(monkeypatch):
    """Make the unlocked inventory slow, as a large history makes it."""

    original = PartitionedStore.hours_newest_first
    entered = threading.Event()

    def slow(self, agent, **kwargs):
        entered.set()
        time.sleep(SLOW_SECONDS)
        return original(self, agent, **kwargs)

    monkeypatch.setattr(PartitionedStore, "hours_newest_first", slow)
    return entered


def _while_retention_runs(expire, write):
    entered_at = {}
    worker = threading.Thread(target=expire)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    write()
    entered_at["write"] = time.monotonic() - started
    worker.join()
    return entered_at["write"]


def test_a_history_write_does_not_wait_for_a_retention_pass(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _slow_inventory(monkeypatch)

    waited = _while_retention_runs(
        lambda: store.expire(cutoff=_at(21, 0)),
        lambda: store.write("codex-api", "row_new", _at(22, 11), {"new": True}),
    )

    assert waited < SLOW_SECONDS / 2
    assert store.find("row_new", agents=["codex-api"], within_days=36500) is not None


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name="codex-api", runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id="project-one", title="Outbox", goal="Retention.", owner="operator")
    return store


def test_an_outbox_settle_does_not_wait_for_outbox_retention(field, monkeypatch):
    first = field.enqueue_service_event("project-one", worker_name="codex-api", kind="note.recorded",
                                        summary="old", source_event_ref="local:w943c:1")
    field.pull_outbox(relay_id="relay-01", worker_name="codex-api")
    field.settle_outbox(first["outbox_id"], relay_id="relay-01", outcome="sent")
    second = field.enqueue_service_event("project-one", worker_name="codex-api", kind="note.recorded",
                                         summary="new", source_event_ref="local:w943c:2")
    field.pull_outbox(relay_id="relay-01", worker_name="codex-api")
    _slow_inventory(monkeypatch)

    waited = _while_retention_runs(
        lambda: maintenance.apply_outbox_retention(field),
        lambda: field.settle_outbox(second["outbox_id"], relay_id="relay-01", outcome="sent"),
    )

    assert waited < SLOW_SECONDS / 2


def test_the_expire_result_and_day_indexes_are_those_of_a_locked_pass(tmp_path):
    store = _store(tmp_path)

    removed = store.expire(cutoff=_at(21, 10), max_records_per_agent=2)

    # Age: day 20 and 21 09:00 go (3 hours); the record bound then drops the oldest left, 21 10:00.
    assert removed == {"partitions": 4, "records": 4}
    root = tmp_path / "history" / "codex-api" / "2026" / "09"
    assert sorted(p.name for p in root.iterdir()) == ["22"]
    assert (root / "22" / "ids").read_text().splitlines() == ["row_22_9 09", "row_22_10 10"]


def test_an_hour_removed_between_a_readers_listing_and_its_read_reads_as_expired(tmp_path, monkeypatch):
    outbox = OutboxStore(tmp_path / "control")
    row = {"outbox_id": "outbox_x", "project_ref": "work:project:project-one", "worker_name": "codex-api",
           "kind": "note.recorded", "created_at": _at(22, 9).isoformat()}
    path = outbox.partitioned("work:project:project-one").write("codex-api", "outbox_x", _at(22, 9), row)
    original = PartitionedStore.find

    def find_then_expire(self, *args, **kwargs):
        found = original(self, *args, **kwargs)
        if found is not None:
            import shutil

            shutil.rmtree(found.parent)
        return found

    monkeypatch.setattr(PartitionedStore, "find", find_then_expire)
    assert outbox.find("outbox_x") is None
    assert outbox.read("outbox_x") is None

    # A listing whose hour vanishes before it is opened skips it.
    store = _store(tmp_path)
    original_hours = PartitionedStore.hours_newest_first

    def hours_then_expire(self, agent, **kwargs):
        for hour, start in original_hours(self, agent, **kwargs):
            if start.day == 22 and start.hour == 10:
                import shutil

                shutil.rmtree(hour)
            yield hour, start

    monkeypatch.setattr(PartitionedStore, "hours_newest_first", hours_then_expire)
    rows = store.newest(op="test", limit=10)
    assert {(row["day"], row["hour"]) for row in rows} == {(22, 9), (21, 10), (21, 9), (20, 10), (20, 9)}
    assert store.latest_stamp("codex-api") is not None
    assert path.name.endswith(".json")


def test_two_retention_passes_at_once_remove_each_hour_once(tmp_path):
    store = _store(tmp_path)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run():
        try:
            barrier.wait()
            results.append(store.expire(cutoff=_at(22, 0)))
        except Exception as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(result["partitions"] for result in results) == 4
    assert sum(result["records"] for result in results) == 4
    remaining = sorted(p.name for p in (tmp_path / "history" / "codex-api" / "2026" / "09").iterdir())
    assert remaining == ["22"]


def test_a_record_written_into_an_hour_after_the_inventory_is_counted(tmp_path, monkeypatch):
    store = _store(tmp_path)
    original = local_store.PartitionedStore._remove_hour

    def write_first(hour):
        if hour.name == "09" and hour.parent.name == "20":
            store._write_unlocked("codex-api", "row_late", _at(20, 9), {"late": True})
        return original(hour)

    monkeypatch.setattr(local_store.PartitionedStore, "_remove_hour", staticmethod(write_first))

    removed = store.expire(cutoff=_at(21, 0))

    assert removed == {"partitions": 2, "records": 3}

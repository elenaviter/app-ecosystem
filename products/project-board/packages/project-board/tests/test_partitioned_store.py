"""Relay-local stores partitioned by agent and hour, read by range (W287 PR 2a).

A read opens only the hour folders its operation needs, and says which, per
agent. ``events`` is the first store on this layout: until W287 every plan
index and every delivery context read all of a project's event files (6,402 on
dev-main) to keep the newest 25 or 50.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_board.client import local_state_maintenance as maintenance
from project_board.client import local_store
from project_board.client.keyed_history import KeyedHistoryStore
from project_board.client.local_store import PartitionedStore, last_read_summaries
from project_board.client.store import SharedFieldStore


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> PartitionedStore:
    store = PartitionedStore(tmp_path / "events", store="events")
    for agent, hour, record_id in (
        ("codex-api", 10, "event_a"),
        ("codex-api", 11, "event_b"),
        ("codex-api", 12, "event_c"),
        ("Claude-Docs", 12, "event_d"),
    ):
        store.write(agent, record_id, _at(hour, 5), {"event_id": record_id, "actor": agent, "created_at": _at(hour, 5).isoformat()})
    return store


def test_a_record_lives_in_its_agent_and_hour_and_is_found_by_id(tmp_path):
    store = _store(tmp_path)

    path = store.find("event_b", agents=["codex-api"], within_days=36500)

    assert path == tmp_path / "events" / "codex-api" / "2026" / "09" / "23" / "11" / "20260923T110500.000000Z_event_b.json"
    assert (tmp_path / "events" / "claude-docs" / "2026" / "09" / "23" / "ids").read_text() == "event_d 12\n"
    assert store.find("event_missing", within_days=36500) is None


def test_an_index_line_without_its_record_waits_for_retention_to_compact(tmp_path, caplog):
    store = _store(tmp_path)
    (tmp_path / "events" / "codex-api" / "2026" / "09" / "23" / "11" / "20260923T110500.000000Z_event_b.json").unlink()

    with caplog.at_level(logging.WARNING, logger=local_store.__name__):
        assert store.find("event_b", agents=["codex-api"], within_days=36500) is None
        assert store.find("event_b", agents=["codex-api"], within_days=36500) is None

    ids = (tmp_path / "events" / "codex-api" / "2026" / "09" / "23" / "ids").read_text().splitlines()
    assert ids == ["event_a 10", "event_b 11", "event_c 12"]
    assert not [r for r in caplog.records if "index rebuilt" in r.getMessage()]

    store.expire(cutoff=_at(1), max_bytes_per_agent=10_000)

    assert (tmp_path / "events" / "codex-api" / "2026" / "09" / "23" / "ids").read_text().splitlines() == [
        "event_a 10",
        "event_c 12",
    ]


def test_newest_opens_only_the_hours_it_needs_and_logs_the_range_per_agent(tmp_path, caplog):
    store = _store(tmp_path)

    with caplog.at_level(logging.INFO, logger=local_store.__name__):
        rows = store.newest(op="list", limit=2)

    # Both newest records share 12:05:00; either order is newest-first.
    assert sorted(row["event_id"] for row in rows) == ["event_c", "event_d"]
    lines = sorted(r.getMessage() for r in caplog.records if r.getMessage().startswith("relay store read"))
    # One line per agent, naming the one hour each opened: the older hours of
    # codex-api were never listed (acceptance 10: no read beyond what it needs).
    assert [re.sub(r" ms=\d+$", "", line) for line in lines] == [
        "relay store read worker=claude-docs store=events op=list range=2026-09-23T12..2026-09-23T12 partitions=1 records=1",
        "relay store read worker=codex-api store=events op=list range=2026-09-23T12..2026-09-23T12 partitions=1 records=1",
    ]
    assert last_read_summaries("codex-api")["events"]["range"] == "2026-09-23T12..2026-09-23T12"


def test_newest_merges_same_hour_agents_before_applying_limit(tmp_path):
    store = PartitionedStore(tmp_path / "events", store="events")
    store.write(
        "z-agent",
        "older",
        _at(10, 1),
        {"event_id": "older", "created_at": _at(10, 1).isoformat()},
    )
    store.write(
        "a-agent",
        "newer",
        _at(10, 59),
        {"event_id": "newer", "created_at": _at(10, 59).isoformat()},
    )

    assert [row["event_id"] for row in store.newest(op="list", limit=1)] == [
        "newer"
    ]


def test_expire_removes_whole_hours_by_name_and_prunes_empty_days(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(local_store, "read_json", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("retention opens no record")))

    removed = store.expire(cutoff=_at(12))

    assert removed == {"partitions": 2, "records": 2}
    assert sorted(p.name for p in (tmp_path / "events" / "codex-api" / "2026" / "09" / "23").iterdir()) == ["12", "ids"]
    store.expire(cutoff=_at(23))
    assert not (tmp_path / "events" / "codex-api" / "2026").exists()


def test_expire_enforces_record_and_byte_bounds_with_oldest_hours_first(tmp_path):
    store = _store(tmp_path)

    removed = store.expire(
        cutoff=_at(1),
        max_records_per_agent=2,
        max_bytes_per_agent=10_000,
    )

    assert removed == {"partitions": 1, "records": 1}
    assert store.find("event_a", agents=["codex-api"], within_days=36500) is None
    assert store.find("event_b", agents=["codex-api"], within_days=36500) is not None
    assert store.find("event_c", agents=["codex-api"], within_days=36500) is not None

    removed = store.expire(
        cutoff=_at(1),
        max_records_per_agent=10,
        max_bytes_per_agent=1,
    )
    assert removed["records"] == 3
    assert list((tmp_path / "events").rglob("*.json")) == []


def test_retention_compacts_stable_key_rewrites_and_counts_index_bytes(
    tmp_path,
):
    history = KeyedHistoryStore(
        tmp_path / "history",
        store="history",
        retention_days=30,
    )
    for minute in range(20):
        history.write(
            agent="codex-api",
            record_id="stable-key",
            created=_at(10, minute),
            row={"record_id": "stable-key", "created_at": _at(10, minute).isoformat()},
        )

    ids = tmp_path / "history" / "codex-api" / "2026" / "09" / "23" / "ids"
    records = list((tmp_path / "history").rglob("*.json"))
    assert ids.read_text().splitlines() == ["stable-key 10"] * 20
    assert len(records) == 1

    compact_size = len("stable-key 10\n".encode("utf-8"))
    removed = history.partitioned.expire(
        cutoff=_at(1),
        max_records_per_agent=1,
        max_bytes_per_agent=records[0].stat().st_size + compact_size,
    )

    assert removed == {"partitions": 0, "records": 0}
    assert ids.read_text().splitlines() == ["stable-key 10"]

    removed = history.partitioned.expire(
        cutoff=_at(1),
        max_records_per_agent=1,
        max_bytes_per_agent=records[0].stat().st_size,
    )

    assert removed == {"partitions": 1, "records": 1}
    assert not ids.exists()


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name, kind in (("codex-api", "codex"), ("claude-docs", "claude-code")):
        store.register_worker(worker_name=name, runtime_kind=kind, capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id="project-one", title="Partitions", goal="Read only what is needed.", owner="operator")
    return store


def test_project_events_are_partitioned_and_read_newest_first(field, monkeypatch):
    for index in range(5):
        field.record_event("project-one", kind="note", summary=f"event {index}", actor="codex-api", work_ref="work:one" if index % 2 else "")
    root = field._project_dir("project-one") / "events"
    assert not list(root.glob("*.json"))
    # The project's own creation event belongs to its owner.
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["codex-api", "operator"]

    tail = field.list_events("project-one", limit=3)
    assert [row["summary"] for row in tail] == ["event 2", "event 3", "event 4"]
    assert [row["summary"] for row in field.list_events("project-one", limit=10, work_ref="work:one")] == ["event 1", "event 3"]

    # The stalled mark reads when each actor last acted from file names alone.
    from project_board.client import store as store_module

    def no_event_bodies(path, *args, **kwargs):
        if "/events/" in str(path):
            raise AssertionError("activity must not open an event")
        return original(path, *args, **kwargs)

    original = store_module.read_json
    monkeypatch.setattr(store_module, "read_json", no_event_bodies)
    monkeypatch.setattr(store_module, "json_records", lambda directory: [] if "outbox" in str(directory) else [])
    activity = field._assignee_last_activity("project-one")
    assert activity["codex-api"].startswith(datetime.now(timezone.utc).strftime("%Y-%m-%dT"))


def test_delivery_context_reads_new_and_migrated_partitioned_journal_receipts(field):
    receipts = field._journal_receipts()
    work_ref = "work:plan:node:20260923T100000Z:w1:one"
    new = {
        "entry_id": "new-entry",
        "work_ref": work_ref,
        "created_at": "2026-09-23T10:00:00Z",
    }
    migrated = {
        "entry_id": "migrated-entry",
        "work_ref": work_ref,
        "created_at": "2026-09-23T11:00:00Z",
    }
    receipts.write("project-one", "new-entry", new)
    legacy = receipts.legacy_path("project-one", "migrated-entry")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(migrated))

    result = receipts.migrate_legacy()
    context = field.project_delivery_context("project-one", work_ref=work_ref)

    assert result["project-one"]["state"] == "complete"
    assert [row["entry_id"] for row in context["journals"]] == [
        "new-entry",
        "migrated-entry",
    ]


def test_a_caller_chosen_event_id_replays_instead_of_duplicating(field):
    first = field.record_event("project-one", event_id="journal-indexed-abc", kind="journal.indexed", summary="indexed", actor="claude-docs")
    again = field.record_event("project-one", event_id="journal-indexed-abc", kind="journal.indexed", summary="indexed", actor="claude-docs")

    assert again["event_ref"] == first["event_ref"]
    assert len(list((field._project_dir("project-one") / "events" / "claude-docs").rglob("*.json"))) == 1


def test_flat_events_from_before_w287_move_into_agent_and_hour_folders(field):
    legacy = field._project_dir("project-one") / "events"
    legacy.mkdir(parents=True, exist_ok=True)
    for index, actor in enumerate(("codex-api", "codex-api", "claude-docs")):
        (legacy / f"event_{index:03d}.json").write_text(json.dumps({
            "event_id": f"event_{index:03d}", "actor": actor, "summary": f"old {index}",
            "created_at": f"2026-09-2{index}T08:00:00Z",
        }))
    assert len(field.list_events("project-one")) == 4  # three flat, plus the project's creation

    result = maintenance.migrate_flat_events(field, "project-one")

    assert result == {
        "state": "complete",
        "moved": {"codex-api": 2, "claude-docs": 1},
        "unreadable": 0,
    }
    assert not list(legacy.glob("*.json"))
    assert (legacy / "codex-api" / "2026" / "09" / "21" / "08" / "20260921T080000.000000Z_event_001.json").is_file()
    assert [row["summary"] for row in field.list_events("project-one")][:3] == ["old 0", "old 1", "old 2"]


def test_event_retention_runs_with_the_hourly_housekeeping(field):
    field.record_event("project-one", kind="note", summary="now", actor="codex-api")
    later = datetime.now(timezone.utc) + timedelta(days=31)

    summary = maintenance.run_local_state_maintenance(field, now=later)

    assert summary["retention"]["events"]["project-one"]["records"] == 2  # this one and the creation
    assert field.list_events("project-one") == []


def test_a_record_name_carries_a_readable_slug_and_is_still_found_by_id(field):
    event = field.record_event("project-one", event_id="journal-indexed-xyz", kind="journal.indexed", summary="Indexed the W287 journal entry", actor="codex-api")

    names = [p.name for p in (field._project_dir("project-one") / "events" / "codex-api").rglob("*.json")]
    slug = event["event_ref"].rsplit(":", 1)[-1]
    assert len(names) == 1 and names[0].endswith(f"_journal-indexed-xyz__{slug}.json")
    assert local_store.record_id_of(names[0]) == "journal-indexed-xyz"
    assert local_store.slug_component("  W287: Relay/Local State!  ") == "w287-relay-local-state"
    again = field.record_event("project-one", event_id="journal-indexed-xyz", kind="journal.indexed", summary="Indexed the W287 journal entry", actor="codex-api")
    assert again["event_ref"] == event["event_ref"]


def test_a_generated_event_id_carries_its_time_and_what_happened(field, monkeypatch):
    event = field.record_event("project-one", kind="note", summary="Relay local state partitioned by agent", actor="codex-api")

    event_id = event["event_id"]
    assert re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z_[0-9a-f]{12}__relay-local-state-partitioned", event_id)
    path = field._events("project-one").find(event_id, agents=["codex-api"], within_days=30)
    assert path is not None and path.name == f"{event_id}.json"
    assert path.parent.name == event_id[9:11]
    # Found from the id alone: no day index is read.
    (path.parent.parent / local_store.IDS_FILE).unlink()
    assert field._events("project-one").find(event_id, agents=["codex-api"], within_days=30) == path


def test_an_unreadable_flat_event_is_kept_aside_not_deleted(field):
    legacy = field._project_dir("project-one") / "events"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "event_broken.json").write_text("{not json")

    maintenance.migrate_flat_events(field, "project-one")

    assert not (legacy / "event_broken.json").exists()
    assert (legacy / ".legacy-unreadable" / "events" / "event_broken.json").read_text() == "{not json"


def test_a_lookup_by_id_logs_at_debug_and_a_listing_at_info(tmp_path, caplog):
    """A lookup runs once per record touched; at INFO it flooded the relay log (2026-09-24)."""

    store = _store(tmp_path)
    with caplog.at_level(logging.INFO, logger=local_store.__name__):
        store.find("event_b", agents=["codex-api"], within_days=36500)
        store.newest(op="list", limit=1)
    ops = [r.getMessage().split(" op=")[1].split(" ")[0] for r in caplog.records if r.getMessage().startswith("relay store read")]
    assert "lookup" not in ops and ops == ["list", "list"]
    # The lookup still reaches the heartbeat summary.
    store.find("event_a", agents=["codex-api"], within_days=36500)
    assert last_read_summaries("codex-api")["events"]["op"] == "lookup"

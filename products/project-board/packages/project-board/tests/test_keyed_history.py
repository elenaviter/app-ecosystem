from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from project_board.client.keyed_history import KeyedHistoryStore, record_time


NOW = datetime(2026, 9, 25, 1, 2, 3, tzinfo=timezone.utc)


def test_keyed_history_writes_under_agent_and_hour_and_reads_one_key(tmp_path: Path):
    store = KeyedHistoryStore(tmp_path / "handled", store="handled", retention_days=30)

    path = store.write(
        agent="Codex-UI",
        record_id="mail-key",
        row={"message_id": "mail-key", "created_at": "2026-09-25T01:02:03Z"},
    )

    assert path == (
        tmp_path
        / "handled"
        / "codex-ui"
        / "2026"
        / "09"
        / "25"
        / "01"
        / "20260925T010203.000000Z_mail-key.json"
    )
    assert store.read(agent="codex-ui", record_id="mail-key") == {
        "message_id": "mail-key",
        "created_at": "2026-09-25T01:02:03Z",
    }


def test_keyed_history_checks_one_explicit_legacy_path_without_listing(tmp_path: Path, monkeypatch):
    store = KeyedHistoryStore(tmp_path / "handled", store="handled", retention_days=30)
    legacy = tmp_path / "legacy" / "mail-key.json"
    legacy.parent.mkdir()
    legacy.write_text('{"message_id":"mail-key"}')

    def no_agents():
        raise AssertionError("an exact legacy lookup must not enumerate history")

    monkeypatch.setattr(store.partitioned, "agents", no_agents)
    assert store.read(
        agent="codex-ui",
        record_id="mail-key",
        legacy_paths=[legacy],
    ) == {"message_id": "mail-key"}


def test_record_time_uses_the_first_valid_utc_field():
    assert record_time(
        {"created_at": "not-a-time", "recorded_at": "2026-09-25T03:02:01+02:00"},
        fallback=NOW,
    ) == datetime(2026, 9, 25, 1, 2, 1, tzinfo=timezone.utc)

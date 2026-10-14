"""W459 C4: the relay's local-work scanner says, read-only, what it did.

C4 asks whether the live relay keeps one scan thread and joins, rather than multiplies,
a running scan when waits are cancelled. On 5 October the running relay exposed nothing
that could answer it (independent review, NOT VERIFIED). ``LocalWorkScanner.stats``
counts on the event loop; the relay writes the counters into ``relay-channels.json``
with its channel status, and ``pb worker inspect`` shows them. These tests hold scans in
the scanner thread and check every counter, the one-thread bound across many cancelled
waits, the snapshot (known numbers only, channels unchanged) and the inspect line.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from project_board.client import relay_channel_status
from project_board.client.local_work_scanner import COUNTERS, LocalWorkScanner, LocalWorkScannerClosed
from project_board.client.relay_channel_status import (
    SCANNER_FIELDS,
    ChannelStatusBook,
    channel_status,
    local_work_scanner_status,
    write_channel_status,
)
from project_board.client.render import _local_work_scanner_line

from relay_helpers import Attendance, supervisor_with_fake_channels, two_channel_host

HOLD_LIMIT_SECONDS = 4.0


def _held():
    """A scan that runs in the scanner thread until the test releases it."""

    release, started = threading.Event(), threading.Event()

    def scan() -> str:
        started.set()
        release.wait(HOLD_LIMIT_SECONDS)
        return "scanned"

    return release, started, scan


async def _until(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0.005)


def test_a_join_and_a_cancelled_wait_are_counted_and_the_scan_completes_once():
    release, started, scan = _held()

    async def scenario():
        scanner = LocalWorkScanner()
        first = asyncio.create_task(scanner.scan("outbox", scan))
        await _until(started)
        joined = asyncio.create_task(scanner.scan("outbox", scan))
        await asyncio.sleep(0.02)
        during = scanner.stats()
        joined.cancel()
        await asyncio.gather(joined, return_exceptions=True)
        release.set()
        assert await first == "scanned"
        after = scanner.stats()
        scanner.close()
        return during, after

    during, after = asyncio.run(scenario())
    assert during["in_flight"] == 1 and during["scanner_threads_alive"] == 1
    assert during["scans_started"] == 1 and during["scans_joined"] == 1 and during["scans_completed"] == 0
    assert {name: after[name] for name in COUNTERS} == {
        "scans_started": 1, "scans_joined": 1, "waits_cancelled": 1, "scans_completed": 1,
        "scans_failed": 0, "scans_refused_closed": 0}
    assert after["in_flight"] == 0
    assert after["last_scan_ms"] > 0 and after["max_scan_ms"] >= after["last_scan_ms"]
    assert after["closed"] is False


def test_fifty_cancelled_waits_on_a_held_scan_keep_one_scan_and_one_thread():
    release, started, scan = _held()

    async def scenario():
        scanner = LocalWorkScanner()
        first = asyncio.create_task(scanner.scan("field", scan))
        await _until(started)
        threads = []
        for _ in range(50):
            wait = asyncio.create_task(scanner.scan("field", scan))
            await asyncio.sleep(0)
            wait.cancel()
            await asyncio.gather(wait, return_exceptions=True)
            stats = scanner.stats()
            threads.append((stats["scanner_threads_alive"], stats["in_flight"]))
        release.set()
        await first
        final = scanner.stats()
        scanner.close()
        return threads, final

    threads, final = asyncio.run(scenario())
    assert max(alive for alive, _ in threads) == 1 and max(flight for _, flight in threads) == 1
    assert final["scans_started"] == 1, "every cancelled wait joined the one running scan"
    assert final["scans_joined"] == 50 and final["waits_cancelled"] == 50
    assert final["scans_completed"] == 1


def test_a_failed_scan_and_scans_refused_by_close_are_counted_apart():
    release, started, scan = _held()

    def failing() -> str:
        raise OSError("field unreadable")

    async def scenario():
        scanner = LocalWorkScanner()
        with pytest.raises(OSError):
            await scanner.scan("failing", failing)
        first = asyncio.create_task(scanner.scan("held", scan))
        await _until(started)
        queued = asyncio.create_task(scanner.scan("queued", lambda: "never"))
        await asyncio.sleep(0.02)
        scanner.close()
        release.set()
        outcome = await asyncio.gather(first, queued, return_exceptions=True)
        with pytest.raises(LocalWorkScannerClosed):
            await scanner.scan("after", lambda: "never")
        return outcome, scanner.stats()

    (first, queued), stats = asyncio.run(scenario())
    assert first == "scanned" and isinstance(queued, LocalWorkScannerClosed)
    assert stats["scans_failed"] == 1 and stats["scans_completed"] == 1
    assert stats["scans_refused_closed"] == 2, "the queued scan, ended in its thread, and the scan asked after close"
    assert stats["scans_started"] == 3 and stats["closed"] is True


def test_the_snapshot_carries_only_the_scanners_known_numbers_and_keeps_the_channels(tmp_path):
    book = ChannelStatusBook(clock=lambda: 1_760_000_000.0)
    book.note_turn("worker-a", "succeeded")
    stats = {**dict.fromkeys(COUNTERS, 3), "in_flight": 1, "scanner_threads_alive": 1, "last_scan_ms": 12.5,
             "max_scan_ms": 40.0, "closed": False,
             "secret": "token-shaped-value", "scans_failed": "free text", "kinds": ["outbox"]}
    snapshot = book.take_snapshot(local_work_scanner=stats)
    assert set(snapshot["local_work_scanner"]) == set(SCANNER_FIELDS) - {"scans_failed"}
    assert "token-shaped-value" not in json.dumps(snapshot) and "free text" not in json.dumps(snapshot)
    assert set(snapshot) == {"schema", "recorded_at", "channels", "local_work_scanner"}
    assert list(snapshot["channels"]) == ["worker-a"] and snapshot["channels"]["worker-a"]["last_outcome"] == "succeeded"

    config = tmp_path / "relay.json"
    write_channel_status(config, snapshot)
    read = local_work_scanner_status(config)
    assert read == {**snapshot["local_work_scanner"], "recorded_at": snapshot["recorded_at"]}
    assert channel_status(config, "worker-a")["last_outcome"] == "succeeded"
    assert ChannelStatusBook().take_snapshot().get("local_work_scanner") is None, "no scanner, no block"


def test_an_older_snapshot_without_the_block_reads_as_not_recorded(tmp_path):
    config = tmp_path / "relay.json"
    write_channel_status(config, ChannelStatusBook().take_snapshot())
    assert local_work_scanner_status(config) is None
    assert local_work_scanner_status(tmp_path / "absent" / "relay.json") is None
    assert _local_work_scanner_line(None) == "local-work scanner: not recorded yet"


def test_the_running_relay_writes_its_scanner_counters_with_the_channel_status(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_channel_status, "WRITE_INTERVAL_SECONDS", 0.0)
    host, fast, slow = two_channel_host(tmp_path)

    async def scenario():
        attendance = Attendance(slow.worker_name)
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.2
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            await supervisor._local_work_scanner.scan("test-kind", lambda: None)
            supervisor._channel_status.note_turn(fast.worker_name, "succeeded")
            for _ in range(3):
                if supervisor._channel_status_write is not None:
                    await supervisor._channel_status_write
                supervisor._write_channel_status_soon()
            if supervisor._channel_status_write is not None:
                await supervisor._channel_status_write
        finally:
            attendance.gate.set()
            await supervisor.aclose()

    asyncio.run(scenario())
    written = local_work_scanner_status(host.path)
    assert written is not None and written["recorded_at"]
    assert written["scans_started"] >= 1 and written["scans_completed"] >= 1
    assert written["scanner_threads_alive"] <= 1
    on_disk = json.loads((Path(host.path).parent / relay_channel_status.CHANNEL_STATUS_FILENAME).read_text())
    assert set(on_disk["local_work_scanner"]) <= set(SCANNER_FIELDS)


def test_inspect_prints_the_scanner_counters():
    line = _local_work_scanner_line({
        "scans_started": 4, "scans_joined": 9, "waits_cancelled": 7, "scans_completed": 4, "scans_failed": 0,
        "scans_refused_closed": 0, "in_flight": 0, "scanner_threads_alive": 1, "last_scan_ms": 210.4,
        "max_scan_ms": 1880.0, "closed": False, "recorded_at": "2026-10-10T10:00:00Z"})
    assert line == ("local-work scanner: started 4 · joined 9 · cancelled waits 7 · completed 4 · failed 0 · "
                    "refused after close 0 · in flight 0 · threads 1 · last 210.4 ms · max 1880.0 ms · "
                    "recorded 2026-10-10T10:00:00Z")

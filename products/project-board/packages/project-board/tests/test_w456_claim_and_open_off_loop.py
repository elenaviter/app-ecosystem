"""W456 criterion 4, Infra's review of cf48 (2026-10-05 13:49Z): the two paths left on the loop.

1. With a pending row, the flush's claim listed and moved rows on the shared
   loop (1.6 s with a slow listing); only the preflight was in a thread.
2. Opening or reopening a channel read its Card on the loop (0.8 s) before
   connecting; only the warm-session check was in a thread.
The first two cases are Infra's own (``test_remaining_hot_paths.py``). The
rest hold W321: a claim cancelled while it runs leaves nothing claimed.
"""

from __future__ import annotations

import asyncio
import threading
import time

from project_board.client import outbox_store, relay

from relay_helpers import Attendance, supervisor_with_fake_channels, two_channel_host
from test_w456_devmain_loop_blockers import _adapter, _watched, field  # noqa: F401 (fixture)


def _pending(field, ref="local:test:claim"):
    field.enqueue_service_event(
        "project-one", worker_name="codex-api", kind="note.recorded", summary="pending row", source_event_ref=ref,
    )


def test_nonempty_outbox_claim_listing_stays_off_shared_loop(field, monkeypatch):
    _pending(field)
    original = outbox_store.OutboxStore.in_flight
    on_loop = []

    def slow_only_on_loop(self, status, **kwargs):
        if threading.current_thread() is threading.main_thread():
            on_loop.append(status)
            time.sleep(0.8)
        yield from original(self, status, **kwargs)

    monkeypatch.setattr(outbox_store.OutboxStore, "in_flight", slow_only_on_loop)
    counts, waited, gap = asyncio.run(_watched(_adapter(field)._flush_outbox_unlocked(kinds={"no-such-kind"})))
    assert counts["outbox_sent"] == 0
    assert gap < 0.5, f"gap={gap:.3f}s elapsed={waited:.3f}s main_thread_in_flight_calls={on_loop!r}"


def test_first_or_reopened_channel_card_read_stays_off_shared_loop(tmp_path, monkeypatch):
    host, fast, slow = two_channel_host(tmp_path)
    original = relay.ProblemBoardRelaySupervisor._card_fingerprint.__func__
    on_loop = []

    def slow_only_on_loop(cls, selected_host, channel):
        if threading.current_thread() is threading.main_thread():
            on_loop.append(channel.worker_name)
            time.sleep(0.8)
        return original(cls, selected_host, channel)

    monkeypatch.setattr(relay.ProblemBoardRelaySupervisor, "_card_fingerprint", classmethod(slow_only_on_loop))

    async def scenario():
        attendance = Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            assert fast.worker_name not in supervisor._sessions
            return await _watched(supervisor._poll_channel(host, fast))
        finally:
            await supervisor.aclose()

    _result, waited, gap = asyncio.run(scenario())
    assert gap < 0.5, f"gap={gap:.3f}s elapsed={waited:.3f}s main_thread_card_reads={on_loop!r}"


def test_the_production_session_open_reads_the_card_off_the_loop(tmp_path, monkeypatch):
    """The real ``_open_session``, not the helper's fake: its connector fails at once."""

    host, fast, _slow = two_channel_host(tmp_path)
    original = relay.ProblemBoardRelaySupervisor._card_fingerprint.__func__
    reads = []

    def slow_read(cls, selected_host, channel):
        reads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.8)
        return original(cls, selected_host, channel)

    monkeypatch.setattr(relay.ProblemBoardRelaySupervisor, "_card_fingerprint", classmethod(slow_read))

    class Refused(Exception):
        pass

    class FailingConnection:
        async def __aenter__(self):
            raise Refused()

        async def __aexit__(self, *_exc):
            return False

    async def scenario():
        supervisor = relay.ProblemBoardRelaySupervisor(
            config_path=host.path, connector=lambda *_a, **_k: FailingConnection(),
        )
        try:
            async def opening():
                try:
                    await supervisor._open_session(host, fast)
                except Refused:
                    return "refused"
            return await _watched(opening())
        finally:
            await supervisor.aclose()

    (outcome, waited, gap) = asyncio.run(scenario())
    assert outcome == "refused", "the open reached its connection after reading the Card"
    assert reads == [False], "the Card was read once, in a thread"
    assert waited >= 0.7
    assert gap < 0.5, f"gap={gap:.3f}s"


def _states(field):
    out = field._outbox
    return {
        "pending": len(list(out.in_flight("pending", worker_name="codex-api"))),
        "leased": len(list(out.in_flight("leased", worker_name="codex-api"))),
    }


def test_a_claim_cancelled_while_its_thread_runs_leaves_nothing_claimed(field, monkeypatch):
    _pending(field, "local:test:cancel-1")
    _pending(field, "local:test:cancel-2")
    started = threading.Event()
    original = type(field).pull_outbox

    def slow_claim(self, **kwargs):
        started.set()
        time.sleep(0.4)
        return original(self, **kwargs)

    claimed = threading.Event()

    def slow_claim_done(self, **kwargs):
        try:
            return slow_claim(self, **kwargs)
        finally:
            claimed.set()

    monkeypatch.setattr(type(field), "pull_outbox", slow_claim_done)
    adapter = _adapter(field)

    async def scenario():
        flush = asyncio.ensure_future(adapter._flush_outbox_unlocked())
        while not started.is_set():
            await asyncio.sleep(0.01)
        flush.cancel()
        try:
            await flush
        except asyncio.CancelledError:
            pass
        # The thread finishes its claim; its done callback starts the release.
        while not claimed.is_set():
            await asyncio.sleep(0.01)
        for _ in range(50):
            if adapter._outbox_finishes:
                break
            await asyncio.sleep(0.01)
        while adapter._outbox_finishes:
            await asyncio.gather(*list(adapter._outbox_finishes), return_exceptions=True)

    asyncio.run(scenario())
    assert _states(field) == {"pending": 2, "leased": 0}, "the cancelled claim returned every row"
    rows = [field._outbox.read(path.stem) for path in field._outbox.in_flight("pending", worker_name="codex-api")]
    assert all(not row.get("retry_count") and not row.get("next_attempt_at") for row in rows), "a release is no retry"


def test_a_claim_cancelled_while_the_lock_is_busy_claims_nothing(field):
    """W321 as before: the store refuses a busy lock before reading or moving a row."""

    from project_board.client.io import exclusive_lock

    _pending(field, "local:test:busy")
    held = threading.Event()
    release = threading.Event()

    def hold():
        with exclusive_lock(field._outbox.lock):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(2)
    adapter = _adapter(field)
    adapter.OUTBOX_LOCK_RETRY_FIRST_SECONDS = 0.01

    async def scenario():
        flush = asyncio.ensure_future(adapter._claim_outbox(relay_id="relay-01", worker_name="codex-api", limit=20))
        await asyncio.sleep(0.2)
        flush.cancel()
        try:
            await flush
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        holder.join()
    assert _states(field) == {"pending": 1, "leased": 0}


def test_a_release_leaves_a_row_leased_to_another_relay(field):
    _pending(field, "local:test:other")
    rows = field.pull_outbox(relay_id="relay-02", worker_name="codex-api")
    assert field.release_outbox_claims([rows[0]["outbox_id"]], relay_id="relay-01") == []
    assert _states(field) == {"pending": 0, "leased": 1}
    assert field.release_outbox_claims([rows[0]["outbox_id"]], relay_id="relay-02") == [rows[0]["outbox_id"]]
    assert _states(field) == {"pending": 1, "leased": 0}

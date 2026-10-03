"""The local session wake never holds the relay's shared event loop (W456).

After the fd7c266 activation on 2026-10-02 the relay watchdog named the
session wake as the remaining loop blocker: 8 of 12 sampled blocks sat under
``_notify_available_input``, the longest a 15.8 s ``read_json`` in
``pending_worker_mail_refs``. The loop lagged 23.6 s, no socket of the host
could answer the server's ping, and the server closed every one of them.

These tests run the real wake and the real mailbox traversal, from both places
the relay starts a wake: beside a channel's turn and in an active turn's
finishing. One channel's mailbox is held by the test (a read that waits for a
gate, or its mailbox lock held by another holder), and every claim about the
other channel is checked while that hold lasts, before the test releases it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from project_board.client import coordinate_queue, host_config, relay
from project_board.client import store as store_module
from project_board.client.io import exclusive_lock
from project_board.client.store import SharedFieldStore
from project_board.contract.worker_identity import WorkerSessionIdentity

from relay_helpers import (
    Attendance,
    LoopHeartbeat,
    make_host,
    make_supervisor,
    submit_request,
    supervisor_with_fake_channels,
    two_channel_host,
)

# How long a held read waits for its gate before it gives up and reads. Only a
# relay whose loop is blocked by the read itself ever reaches it: its test
# cannot open the gate, so the read runs this long and the test then fails on
# its timings instead of hanging.
GATE_LIMIT_SECONDS = 4.0
PEER_LIMIT_SECONDS = 2.0
MAX_LOOP_GAP_SECONDS = 0.3


class MailboxGate:
    """Every mailbox read of the named workers waits until the test opens the gate."""

    def __init__(self, monkeypatch, *worker_names: str, error: Exception | None = None) -> None:
        self.mailboxes = {name.lower() for name in worker_names}
        self.error = error
        self.entered = threading.Event()
        self.entered_by: set[str] = set()
        self.opened = threading.Event()
        self.finished: list[float] = []
        real_read_json = store_module.read_json

        def read_json(path, *args, **kwargs):
            parts = Path(path).parts
            held = self.mailboxes.intersection(parts)
            if held and Path(path).parent.name in {"inbox", "leased"}:
                self.entered_by.update(held)
                self.entered.set()
                self.opened.wait(GATE_LIMIT_SECONDS)  # a read on a paging or contended disk
                self.finished.append(time.monotonic())
                if self.error is not None:
                    raise self.error
            return real_read_json(path, *args, **kwargs)

        monkeypatch.setattr(store_module, "read_json", read_json)

    async def wait_entered(self, count: int = 1) -> None:
        deadline = time.monotonic() + GATE_LIMIT_SECONDS + 1
        while len(self.entered_by) < count:
            assert time.monotonic() < deadline, "the held read never started"
            await asyncio.sleep(0.01)


def _register(field: SharedFieldStore, identity: WorkerSessionIdentity, mail: int) -> None:
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    field.listen_worker(identity.worker_name, check_interval_seconds=30)
    for number in range(mail):
        field.send_mail(
            "", sender="control-plane", recipient=identity.worker_name, kind="request",
            subject=f"Pending {number}", body="Waiting.",
            idempotency_key=f"pending-{identity.worker_name}-{number}",
        )


def _identity(channel) -> WorkerSessionIdentity:
    return WorkerSessionIdentity.create(channel.runtime_kind, channel.runtime_session_id)


CODEX_SESSIONS = [f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}" for digit in "23456789"]


def _codex_channels(tmp_path: Path, count: int, *, first_mail: int = 1):
    """``count`` Codex channels on one host, each listening with pending mail."""

    host, first, _channel = make_host(tmp_path)
    identities = [first]
    for session in CODEX_SESSIONS[: count - 1]:
        identity = WorkerSessionIdentity.create("codex", session)
        host_config.enroll_worker_channel(
            host.path, identity=identity, profile=f"problem-board-codex-{session[:4]}", authorized=True
        )
        identities.append(identity)
    host = host_config.HostRelayConfig.load(host.path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w456-notify")
    for index, identity in enumerate(identities):
        _register(field, identity, first_mail if index == 0 else 1)
    return host, field, [host.worker(identity) for identity in identities]


def _two_codex_channels(tmp_path: Path, *, slow_mail: int = 1):
    """Two Codex channels on one host, each listening with pending mail."""

    host, field, (slow, peer) = _codex_channels(tmp_path, 2, first_mail=slow_mail)
    return host, field, slow, peer


def _lease_one_live_and_one_expired(field: SharedFieldStore, worker_name: str) -> tuple[str, str]:
    """Lease two of the worker's messages and let the second lease expire."""

    live, expired = field.pull_mail("", worker_name=worker_name, lease_owner="test", limit=2)
    leased = field._mail_root("", worker_name) / "leased"
    for path in leased.glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["message_ref"] == expired["message_ref"]:
            row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
            path.write_text(json.dumps(row), encoding="utf-8")
    return live["message_ref"], expired["message_ref"]


def _with_session_stubs(supervisor, pushed: list[str]):
    """The native queue is outside the relay: its push and listing are recorded."""

    def notifier(channel, **_kwargs: Any) -> dict[str, Any]:
        pushed.append(channel.worker_name)
        return {"adapter": "codex-queue", "state": "attached", "delivered": True,
                "queued_submission_id": f"sub-{channel.worker_name}"}

    def reconciler(_channel, **_kwargs: Any) -> dict[str, Any]:
        return {"reconciled": True, "queued_submission_ids": []}

    supervisor.session_notifier = notifier
    supervisor.session_queue_reconciler = reconciler
    return supervisor


def _assert_wake_carries_the_mail(field, channel, delivery, expected: list[str]) -> None:
    assert delivery["delivered"] is True
    subscription = field.worker_listener_session(channel.worker_name)["subscription"]
    assert subscription["outstanding_wake_id"] == delivery["wake_id"]
    assert subscription["last_wake_message_refs"] == expected
    assert field.pending_worker_mail_refs(channel.worker_name) == expected, (
        "the wake leaves its mail pending for receive"
    )


def test_a_held_mail_read_leaves_the_loop_and_a_peer_wake_running(tmp_path, monkeypatch):
    """Beside-turn wakes; the held channel also has a live and an expired lease."""

    host, field, slow, peer = _two_codex_channels(tmp_path, slow_mail=3)
    live_ref, expired_ref = _lease_one_live_and_one_expired(field, slow.worker_name)
    peer_expected = field.pending_worker_mail_refs(peer.worker_name)
    gate = MailboxGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    async def scenario():
        beat = LoopHeartbeat()
        beat.start()
        # As the relay cycle starts them: one wake task per channel, not awaited there.
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        supervisor._start_notify_beside_turn(host, peer, operation="input.available")
        slow_task = supervisor._beside_notifies[slow.worker_name]
        peer_task = supervisor._beside_notifies[peer.worker_name]
        await gate.wait_entered()
        held_at = time.monotonic()
        peer_delivery = await asyncio.wait_for(asyncio.shield(peer_task), PEER_LIMIT_SECONDS + 5)
        peer_seconds = time.monotonic() - held_at
        still_held = not gate.opened.is_set() and not slow_task.done()
        gap_while_held = beat.max_gap
        gate.opened.set()
        slow_delivery = await slow_task
        await beat.stop()
        return peer_delivery, peer_seconds, still_held, gap_while_held, slow_delivery

    peer_delivery, peer_seconds, still_held, gap_while_held, slow_delivery = asyncio.run(scenario())

    assert still_held, "the peer was checked while the slow read was still held"
    assert peer_seconds < PEER_LIMIT_SECONDS, f"the peer's wake waited {peer_seconds:.2f}s on another channel"
    assert gap_while_held < MAX_LOOP_GAP_SECONDS, f"the event loop stalled {gap_while_held:.2f}s on a mail read"
    _assert_wake_carries_the_mail(field, peer, peer_delivery, peer_expected)
    # The held channel's wake is the same wake: the expired lease came back to
    # pending and is in it, the live lease is still held and is not.
    slow_expected = field.pending_worker_mail_refs(slow.worker_name)
    assert expired_ref in slow_expected and live_ref not in slow_expected
    assert len(slow_expected) == 2
    _assert_wake_carries_the_mail(field, slow, slow_delivery, slow_expected)
    assert sorted(pushed) == sorted([slow.worker_name, peer.worker_name]), "one push per channel"


def test_a_held_mailbox_lock_leaves_the_loop_and_a_peer_wake_running(tmp_path, monkeypatch):
    host, field, slow, peer = _two_codex_channels(tmp_path)
    slow_expected = field.pending_worker_mail_refs(slow.worker_name)
    lock_path = field._mail_root("", slow.worker_name) / ".mail.lock"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        # Another holder of the mailbox lock: a pb command or the maintenance thread.
        with exclusive_lock(lock_path):
            held.set()
            release.wait(GATE_LIMIT_SECONDS)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    async def scenario():
        beat = LoopHeartbeat()
        beat.start()
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        supervisor._start_notify_beside_turn(host, peer, operation="input.available")
        slow_task = supervisor._beside_notifies[slow.worker_name]
        peer_task = supervisor._beside_notifies[peer.worker_name]
        started = time.monotonic()
        await asyncio.wait_for(asyncio.shield(peer_task), PEER_LIMIT_SECONDS + 5)
        peer_seconds = time.monotonic() - started
        still_held = not release.is_set() and not slow_task.done()
        gap_while_held = beat.max_gap
        release.set()
        slow_delivery = await slow_task
        await beat.stop()
        return peer_seconds, still_held, gap_while_held, slow_delivery

    peer_seconds, still_held, gap_while_held, slow_delivery = asyncio.run(scenario())
    thread.join(2)

    assert still_held, "the peer was checked while the mailbox lock was still held"
    assert peer_seconds < PEER_LIMIT_SECONDS
    assert gap_while_held < MAX_LOOP_GAP_SECONDS, f"the event loop stalled {gap_while_held:.2f}s on a mailbox lock"
    _assert_wake_carries_the_mail(field, slow, slow_delivery, slow_expected)


def test_held_mailboxes_beyond_the_default_pool_leave_a_peer_and_the_pool_free(tmp_path, monkeypatch):
    """Each channel's wake has its own thread: held mailboxes never fill a shared pool."""

    host, field, channels = _codex_channels(tmp_path, 4)
    held, peer = channels[:3], channels[3]
    gate = MailboxGate(monkeypatch, *(channel.worker_name for channel in held))
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    async def scenario():
        # Fewer default-pool threads than held mailboxes, as on a small host
        # (Python's default is the CPU count plus four).
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        for channel in (*held, peer):
            supervisor._start_notify_beside_turn(host, channel, operation="input.available")
        tasks = {channel.worker_name: supervisor._beside_notifies[channel.worker_name] for channel in channels}
        await gate.wait_entered(2)
        started = time.monotonic()
        await asyncio.wait_for(asyncio.shield(tasks[peer.worker_name]), PEER_LIMIT_SECONDS + 5)
        peer_seconds = time.monotonic() - started
        pool_answer = await asyncio.wait_for(asyncio.to_thread(lambda: "free"), PEER_LIMIT_SECONDS + 5)
        pool_seconds = time.monotonic() - started
        still_held = not gate.opened.is_set()
        entered = set(gate.entered_by)
        gate.opened.set()
        deliveries = await asyncio.gather(*tasks.values())
        return peer_seconds, pool_answer, pool_seconds, still_held, entered, deliveries

    peer_seconds, pool_answer, pool_seconds, still_held, entered, deliveries = asyncio.run(scenario())

    assert still_held, "the peer and the pool were checked while every held mailbox was held"
    assert peer_seconds < PEER_LIMIT_SECONDS, f"the peer's wake waited {peer_seconds:.2f}s for a thread"
    assert pool_answer == "free" and pool_seconds < PEER_LIMIT_SECONDS, "the default pool stayed free"
    assert entered == {channel.worker_name.lower() for channel in held}, "every held mailbox had its own thread"
    assert all(delivery["delivered"] is True for delivery in deliveries)
    assert sorted(pushed) == sorted(channel.worker_name for channel in channels)
    supervisor._store_executors.shutdown()


def _active_turn_host(tmp_path: Path):
    """The fake-session relay harness, with both channels registered and listening."""

    host, fast, slow = two_channel_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w456-notify-turns")
    _register(field, _identity(fast), 1)
    _register(field, _identity(slow), 1)
    return host, field, fast, slow


def test_a_held_mail_read_in_an_active_turns_finishing_delays_only_its_channel(tmp_path, monkeypatch):
    """The relay runtime's own loop: attendance, drop and reopen, coordinate, at production pacing."""

    host, _field, fast, slow = _active_turn_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    gate = MailboxGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, opened = supervisor_with_fake_channels(host, attendance, real_notify=True)
        _with_session_stubs(supervisor, pushed)

        async def run_relay():
            while True:
                await supervisor.poll_once()
                await asyncio.sleep(0.05)

        relay_loop = asyncio.create_task(run_relay())
        beat = LoopHeartbeat()
        try:
            await gate.wait_entered()
            beat.start()
            polls_at_hold = attendance.polls.get(fast.worker_name, 0)
            # The fast channel's socket drops while the slow channel's finishing reads its mail.
            await supervisor._drop_session(fast.worker_name)
            request = submit_request(queue, fast)
            started = time.monotonic()
            response = None
            while response is None and time.monotonic() - started < PEER_LIMIT_SECONDS:
                await asyncio.sleep(0.02)
                response = queue.take_response(
                    worker_name=fast.worker_name, request_id=request["request_id"]
                )
            served_after = time.monotonic() - started
            # A fast answer can arrive before the relay's next 50 ms poll, so
            # keep watching, still inside the peer limit and only while the
            # slow read stays held, for the fast channel's attendance poll.
            while (
                attendance.polls.get(fast.worker_name, 0) == polls_at_hold
                and not gate.opened.is_set()
                and time.monotonic() - started < PEER_LIMIT_SECONDS
            ):
                await asyncio.sleep(0.02)
            polled_while_held = attendance.polls.get(fast.worker_name, 0) - polls_at_hold
            still_held = not gate.opened.is_set()
            reopened = opened.count(fast.worker_name)
            await beat.stop()
        finally:
            gate.opened.set()
            relay_loop.cancel()
            await asyncio.gather(relay_loop, return_exceptions=True)
            await supervisor.aclose()
        return response, served_after, polled_while_held, still_held, reopened, beat.max_gap

    response, served_after, polled_while_held, still_held, reopened, max_gap = asyncio.run(scenario())

    assert still_held, "the fast channel was checked while the slow read was still held"
    assert response is not None and response["ok"] is True, "the dropped channel's call waited on the slow read"
    assert served_after < PEER_LIMIT_SECONDS
    assert reopened >= 2, "the dropped channel reopened"
    assert polled_while_held >= 1, "the fast channel kept polling attendance"
    assert max_gap < MAX_LOOP_GAP_SECONDS, f"the event loop stalled {max_gap:.2f}s"


def test_a_relay_shutdown_during_a_held_read_ends_when_the_read_ends(tmp_path, monkeypatch):
    host, _field, _fast, slow = _active_turn_host(tmp_path)
    gate = MailboxGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, _opened = supervisor_with_fake_channels(host, attendance, real_notify=True)
        _with_session_stubs(supervisor, pushed)
        cycle = asyncio.create_task(supervisor.poll_once())
        await gate.wait_entered()
        threading.Timer(0.3, gate.opened.set).start()
        started = time.monotonic()
        await asyncio.gather(cycle, return_exceptions=True)
        await asyncio.wait_for(supervisor.aclose(), GATE_LIMIT_SECONDS + 5)
        closed = time.monotonic()
        turns_left = [task for task in supervisor._channel_turns.values() if not task.done()]
        return started, closed, turns_left

    started, closed, turns_left = asyncio.run(scenario())

    assert gate.finished and gate.finished[0] <= closed, "shutdown waited for the read in flight"
    assert closed - started < PEER_LIMIT_SECONDS, "and ended as soon as the read ended"
    assert turns_left == []


def test_a_wake_cancelled_during_a_held_read_keeps_its_channel_until_the_read_ends(tmp_path, monkeypatch):
    host, field, slow, _peer = _two_codex_channels(tmp_path)
    gate = MailboxGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    async def scenario():
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        task = supervisor._beside_notifies[slow.worker_name]
        lock = supervisor._notify_lock(slow.worker_name)
        await gate.wait_entered()
        cancelling = asyncio.create_task(supervisor._cancel_notify_beside_turn(slow.worker_name))
        await asyncio.sleep(0.2)
        # The read still runs in its thread: the channel stays held, and no
        # second wake for it starts beside a write the first may still make.
        held_while_reading = lock.locked()
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        restarted = slow.worker_name in supervisor._beside_notifies
        gate.opened.set()
        await cancelling
        ended = time.monotonic()
        return task, held_while_reading, restarted, ended, lock.locked()

    task, held_while_reading, restarted, ended, held_after = asyncio.run(scenario())

    assert task.cancelled()
    assert held_while_reading and not restarted
    assert gate.finished and gate.finished[0] <= ended, "the cancellation waited for the read in flight"
    assert not held_after
    assert pushed == [], "a cancelled wake pushes nothing"
    subscription = field.worker_listener_session(slow.worker_name)["subscription"]
    assert not subscription.get("outstanding_wake_id"), "and records no wake"
    assert field.pending_worker_mail_refs(slow.worker_name), "the mail stays pending"


def test_a_failing_mail_read_is_reported_for_its_channel_only(tmp_path, monkeypatch, caplog):
    host, _field, slow, peer = _two_codex_channels(tmp_path)
    gate = MailboxGate(monkeypatch, slow.worker_name, error=OSError("disk read failed"))
    gate.opened.set()
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    with pytest.raises(OSError, match="disk read failed"):
        asyncio.run(supervisor._notify_available_input(host, slow))

    async def beside():
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        supervisor._start_notify_beside_turn(host, peer, operation="input.available")
        tasks = [supervisor._beside_notifies[channel.worker_name] for channel in (slow, peer)]
        return await asyncio.gather(*tasks)

    with caplog.at_level(logging.WARNING, logger=relay.logger.name):
        slow_delivery, peer_delivery = asyncio.run(beside())

    assert slow_delivery is None
    failures = [record for record in caplog.records if "session wake failed" in record.getMessage()]
    assert [slow.worker_name in record.getMessage() for record in failures] == [True]
    assert failures[0].exc_info and isinstance(failures[0].exc_info[1], OSError)
    assert peer_delivery["delivered"] is True and pushed == [peer.worker_name]

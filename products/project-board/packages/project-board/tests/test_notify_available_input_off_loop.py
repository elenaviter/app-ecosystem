# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W476: the wake's second, post-quota mailbox read runs off the event loop.

``_notify_available_input`` reads a channel's pending mail twice: once before
the quota read and once after it, because that read can yield and the mail or
the listener may change meanwhile. The first read was moved off the loop
(W456); the second, with its listener read and hold writes, still ran on it.
A running relay (2026-10-03, 04:21-04:51Z) logged three event-loop blocks of
about 3.4 s, each in that second read.

The W456 tests hold every mailbox read of the slow channel, so its wake stops
in the first read, off the loop; when the gate opens the second read passes at
once. These tests hold only the second read.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from project_board.client.store import SharedFieldStore

from relay_helpers import LoopHeartbeat, make_supervisor
from test_w456_notify_off_loop import (
    GATE_LIMIT_SECONDS,
    MAX_LOOP_GAP_SECONDS,
    PEER_LIMIT_SECONDS,
    _assert_wake_carries_the_mail,
    _lease_one_live_and_one_expired,
    _two_codex_channels,
    _with_session_stubs,
)


class SecondReadGate:
    """The named worker's second pending-mail read waits until the test opens the gate.

    It also records, for every store call of the wake tail, whether it ran on
    the event loop's thread.
    """

    TAIL = ("pending_worker_mail_refs", "worker_listener_session", "clear_wake_hold", "record_wake_hold")

    def __init__(self, monkeypatch, worker_name: str, *, on_hold=None) -> None:
        self.worker_name = worker_name
        self.on_hold = on_hold
        self.reads = 0
        self.entered = threading.Event()
        self.opened = threading.Event()
        self.loop_thread: int | None = None
        self.on_loop: list[str] = []
        for name in self.TAIL:
            self._wrap(monkeypatch, name)

    def _wrap(self, monkeypatch, name: str) -> None:
        real = getattr(SharedFieldStore, name)
        gate = self

        def wrapped(store, worker_name, *args, **kwargs):
            if worker_name == gate.worker_name:
                if threading.get_ident() == gate.loop_thread:
                    gate.on_loop.append(name)
                if name == "pending_worker_mail_refs":
                    gate.reads += 1
                    if gate.reads == 2:
                        gate.entered.set()
                        if gate.on_hold is not None:
                            gate.on_hold(store)
                        gate.opened.wait(GATE_LIMIT_SECONDS)
            return real(store, worker_name, *args, **kwargs)

        monkeypatch.setattr(SharedFieldStore, name, wrapped)

    async def wait_entered(self) -> None:
        deadline = time.monotonic() + GATE_LIMIT_SECONDS + 1
        while not self.entered.is_set():
            assert time.monotonic() < deadline, "the second read never started"
            await asyncio.sleep(0.01)


def _run_held(host, slow, peer, gate: SecondReadGate, supervisor):
    async def scenario():
        gate.loop_thread = threading.get_ident()
        beat = LoopHeartbeat()
        beat.start()
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

    return asyncio.run(scenario())


def test_a_held_second_mail_read_leaves_the_loop_and_a_peer_wake_running(tmp_path, monkeypatch):
    host, field, slow, peer = _two_codex_channels(tmp_path, slow_mail=3)
    live_ref, expired_ref = _lease_one_live_and_one_expired(field, slow.worker_name)
    peer_expected = field.pending_worker_mail_refs(peer.worker_name)
    gate = SecondReadGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    peer_delivery, peer_seconds, still_held, gap_while_held, slow_delivery = _run_held(
        host, slow, peer, gate, supervisor
    )

    assert still_held, "the peer was checked while the second read was still held"
    assert peer_seconds < PEER_LIMIT_SECONDS, f"the peer's wake waited {peer_seconds:.2f}s on another channel"
    assert gap_while_held < MAX_LOOP_GAP_SECONDS, f"the event loop stalled {gap_while_held:.2f}s on the second read"
    assert gate.on_loop == [], f"wake-tail store calls ran on the event loop: {gate.on_loop}"
    _assert_wake_carries_the_mail(field, peer, peer_delivery, peer_expected)
    # The expired lease came back to pending and is in the wake; the live one is not.
    slow_expected = field.pending_worker_mail_refs(slow.worker_name)
    assert expired_ref in slow_expected and live_ref not in slow_expected
    _assert_wake_carries_the_mail(field, slow, slow_delivery, slow_expected)
    assert sorted(pushed) == sorted([slow.worker_name, peer.worker_name])


def test_a_wake_cancelled_during_the_held_second_read_keeps_its_channel_and_pushes_nothing(tmp_path, monkeypatch):
    # Same rule as the first read (W456): the cancelled wake keeps its
    # channel until the read in flight ends, then pushes and records nothing.
    host, field, slow, _peer = _two_codex_channels(tmp_path, slow_mail=2)
    slow_expected = field.pending_worker_mail_refs(slow.worker_name)
    gate = SecondReadGate(monkeypatch, slow.worker_name)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)

    async def scenario():
        gate.loop_thread = threading.get_ident()
        beat = LoopHeartbeat()
        beat.start()
        supervisor._start_notify_beside_turn(host, slow, operation="input.available")
        task = supervisor._beside_notifies[slow.worker_name]
        lock = supervisor._notify_lock(slow.worker_name)
        await gate.wait_entered()
        cancelling = asyncio.create_task(supervisor._cancel_notify_beside_turn(slow.worker_name))
        await asyncio.sleep(0.2)
        held_while_reading = lock.locked()
        gap_while_held = beat.max_gap
        gate.opened.set()
        await cancelling
        await beat.stop()
        return task, held_while_reading, gap_while_held, lock.locked()

    task, held_while_reading, gap_while_held, held_after = asyncio.run(scenario())

    # Checked before the test's own store reads below, which run on this thread.
    assert gate.on_loop == []
    assert task.cancelled()
    assert held_while_reading and not held_after
    assert gap_while_held < MAX_LOOP_GAP_SECONDS, f"the event loop stalled {gap_while_held:.2f}s"
    assert pushed == []
    subscription = field.worker_listener_session(slow.worker_name)["subscription"]
    assert not subscription.get("outstanding_wake_id"), "and records no wake"
    assert field.pending_worker_mail_refs(slow.worker_name) == slow_expected, "the mail stays pending"


# The quota-held path: the second read, its listener read and the hold write ----


def _quota_held(tmp_path, monkeypatch):
    from test_w438_quota_redemption import _held

    return _held(tmp_path, monkeypatch)


def _open_gate(monkeypatch, worker_name: str) -> SecondReadGate:
    gate = SecondReadGate(monkeypatch, worker_name)
    gate.opened.set()
    return gate


def _run_on_loop(gate: SecondReadGate, coroutine_factory):
    async def scenario():
        gate.loop_thread = threading.get_ident()
        return await coroutine_factory()

    return asyncio.run(scenario())


def test_a_failed_quota_read_records_its_hold_off_the_loop_and_spends_no_turn(tmp_path, monkeypatch):
    from project_board.contract.errors import DomainError

    host, identity, channel, field, supervisor, pushes = _quota_held(tmp_path, monkeypatch)
    gate = _open_gate(monkeypatch, identity.worker_name)

    async def unavailable(**_kwargs: Any):
        raise DomainError("work_codex_quota_account_mismatch", "Account mismatch.")

    supervisor._codex_quota_reader = unavailable
    result = _run_on_loop(gate, lambda: supervisor._notify_available_input(host, channel))

    assert result["wake_deferred"] and result["reason"] == "work_codex_quota_account_mismatch"
    assert pushes == []
    assert field.wake_hold(identity.worker_name), "the hold is recorded"
    assert gate.reads == 2 and gate.on_loop == [], f"wake-tail store calls ran on the event loop: {gate.on_loop}"


def test_a_listener_detached_during_the_quota_read_is_not_woken(tmp_path, monkeypatch):
    from test_w438_quota_redemption import _capacity

    host, identity, channel, field, supervisor, pushes = _quota_held(tmp_path, monkeypatch)
    gate = _open_gate(monkeypatch, identity.worker_name)

    async def detaching(**_kwargs: Any):
        field.detach_worker_listener(identity.worker_name)
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = detaching
    _run_on_loop(gate, lambda: supervisor._notify_available_input(host, channel))

    assert pushes == []
    assert not field.wake_hold(identity.worker_name), "a detached listener keeps no hold"
    assert gate.on_loop == []


def test_a_listener_record_gone_during_the_quota_read_ends_the_wake_like_the_first_read(tmp_path, monkeypatch):
    from project_board.contract.errors import DomainError
    from test_w438_quota_redemption import _capacity

    host, identity, channel, field, supervisor, pushes = _quota_held(tmp_path, monkeypatch)
    gate = _open_gate(monkeypatch, identity.worker_name)
    gone = {"after_quota": False}
    listener_read = SharedFieldStore.worker_listener_session

    def worker_listener_session(store, worker_name, *args, **kwargs):
        if gone["after_quota"] and worker_name == identity.worker_name:
            raise DomainError("field_record_not_found", "The listener record is gone.")
        return listener_read(store, worker_name, *args, **kwargs)

    monkeypatch.setattr(SharedFieldStore, "worker_listener_session", worker_listener_session)

    async def removing(**_kwargs: Any):
        gone["after_quota"] = True
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = removing
    result = _run_on_loop(gate, lambda: supervisor._notify_available_input(host, channel))

    assert result is None and pushes == []
    assert not field.wake_hold(identity.worker_name)

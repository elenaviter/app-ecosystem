"""Synthetic queued-wake scans cannot monopolize coordinate authority reads.

All mail and profiles belong to tmp_path; no live channel or runtime is used.
Cold and repeated scans block a real inbox JSON read while the production
coordinate server must claim, fence, dispatch and complete on the same channel.
This proves executor isolation, not live C7 or reduced filesystem work.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from project_board.client import coordinate_queue, relay
from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore
from relay_helpers import Attendance, submit_request, supervisor_with_fake_channels, two_channel_host


@pytest.fixture
def queued_mail(tmp_path):
    host, channel, _other = two_channel_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w456-synthetic-scan")
    field.register_worker(
        worker_name=channel.worker_name, worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    field.listen_worker(channel.worker_name, check_interval_seconds=30)
    field.create_project(project_id="scan-project", title="Synthetic scan", goal="Isolation test", owner="operator")
    field.sync_worker_attendances(channel.worker_name, ["work:project:scan-project"])
    quiet = [field.send_mail(
        "scan-project", sender="control-plane", recipient=channel.worker_name, kind="update",
        subject=f"Synthetic quiet notice {number}", body="Information only.",
        payload={"expected_reaction": "acknowledge_only"}, idempotency_key=f"quiet-{number}",
    )["message_ref"] for number in range(300)]
    actionable = field.send_mail(
        "", sender="control-plane", recipient=channel.worker_name, kind="request",
        subject="Synthetic actionable request", body="Please act.", idempotency_key="action",
    )["message_ref"]
    wake = "wake_0000000000000000000000000000f456"
    field.prepare_worker_session_wake(channel.worker_name, message_refs=[actionable], wake_id=wake)
    field.record_worker_session_delivery(
        channel.worker_name, adapter="codex-queue", state="attached", event_kind="input.available",
        delivered=True, message_refs=[actionable], wake_id=wake, prepared=True,
        queued_submission_id="synthetic-submission",
    )
    return host, channel, field, quiet, actionable


def _notification_stubs(supervisor, monkeypatch):
    async def reconcile(*_args, **_kwargs):
        return None

    async def quota(_field, _channel, limit_state, **_kwargs):
        return limit_state, ""

    async def notify(*_args, **_kwargs):
        raise AssertionError("a queued wake must not be pushed twice")

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_refresh_codex_quota", quota)
    monkeypatch.setattr(supervisor, "_notify_session", notify)
    monkeypatch.setattr(relay, "session_with_limit_state", lambda listener, **_: {
        **listener, "limit_state": {"kind": "ok", "resets_at": ""},
    })


def _block_scan(monkeypatch, method):
    """Block a real read in only the named scan, not an authority read."""

    blocked, release = threading.Event(), threading.Event()
    reads: list[str] = []
    real_read = store_module.read_json
    real_scan = getattr(SharedFieldStore, method)
    context = threading.local()

    def scan(self, *args, **kwargs):
        context.scanning = True
        try:
            return real_scan(self, *args, **kwargs)
        finally:
            context.scanning = False

    def slow_inbox_read(path, *args, **kwargs):
        if getattr(context, "scanning", False) and path.parent.name == "inbox" and not release.is_set():
            reads.append(path.name)
            blocked.set()
            release.wait(10)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(SharedFieldStore, method, scan)
    monkeypatch.setattr(store_module, "read_json", slow_inbox_read)
    return blocked, release, reads


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "repeated"])
@pytest.mark.parametrize("method", ["inbox_refs_not_in", "quiet_mail_refs"], ids=["discovery", "classification"])
def test_queued_quiet_scan_does_not_hold_same_channel_coordinate(queued_mail, monkeypatch, warm, method):
    host, channel, field, quiet, actionable = queued_mail

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        queue = coordinate_queue.CoordinateQueue(host.field_root)
        scan = server = None
        response = None
        elapsed = 0.0
        release = None
        try:
            await supervisor.poll_once()
            client = supervisor._sessions[channel.worker_name].client
            _notification_stubs(supervisor, monkeypatch)

            async def notify():
                return await relay.ProblemBoardRelaySupervisor._notify_available_input(supervisor, host, channel)

            if warm:
                assert (await notify())["deduplicated"] is True
                # Repeated classification must discover fresh mail as well as
                # honor the existing quiet token; do not manufacture a cache hit.
                quiet.append(field.send_mail(
                    "scan-project", sender="control-plane", recipient=channel.worker_name, kind="update",
                    subject="Later quiet notice", body="Information only.",
                    payload={"expected_reaction": "acknowledge_only"}, idempotency_key="quiet-later",
                )["message_ref"])
            blocked, release, reads = _block_scan(monkeypatch, method)
            scan = asyncio.create_task(notify())
            assert await asyncio.to_thread(blocked.wait, 3), "the real inbox read was reached"
            request = submit_request(queue, channel)
            started = time.monotonic()
            server = asyncio.create_task(supervisor._serve_coordinate_requests())
            while response is None and time.monotonic() - started < 2:
                await asyncio.sleep(0.01)
                response = queue.take_response(worker_name=channel.worker_name,
                                               request_id=request["request_id"])
            elapsed = time.monotonic() - started
        finally:
            # Never cancel a thread-backed scan while its fabricated read is
            # blocked: run_off_loop correctly joins it on cancellation.
            if release is not None:
                release.set()
            if scan is not None:
                delivery = await scan
            if server is not None:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
            await supervisor.aclose()
        assert reads, "the scan must use real uncached JSON reads"
        assert response is not None and response["ok"] is True, (
            "a read-only quiet-mail scan held the same channel's coordinate executor")
        assert elapsed < 2, f"coordinate took {elapsed:.3f}s"
        assert delivery["deduplicated"] is True
        assert set(field.pending_worker_mail_refs(channel.worker_name)) == {*quiet, actionable}
        subscription = field.worker_listener_session(channel.worker_name)["subscription"]
        assert subscription["last_wake_message_refs"] == [actionable], "quiet mail never replaces actionable refs"
        assert supervisor._quiet_classified[channel.worker_name] == {
            **{ref: True for ref in quiet}, actionable: False,
        }
        assert len(client.calls) == 1
        assert client.calls[0]["transport_request_id"] == request["request_id"]
        assert client.calls[0]["payload"] == {"item_key": "W1"}

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["inbox_refs_not_in", "quiet_mail_refs"], ids=["discovery", "classification"])
def test_cancelled_scan_keeps_notify_lock_until_real_read_finishes(queued_mail, monkeypatch, method):
    host, channel, field, quiet, actionable = queued_mail

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        task = None
        release = None
        try:
            await supervisor.poll_once()
            _notification_stubs(supervisor, monkeypatch)
            monkeypatch.setattr(supervisor, "_notify_available_input",
                                relay.ProblemBoardRelaySupervisor._notify_available_input.__get__(supervisor))
            blocked, release, _reads = _block_scan(monkeypatch, method)
            supervisor._start_notify_beside_turn(host, channel, operation="input.available")
            task = supervisor._beside_notifies[channel.worker_name]
            assert await asyncio.to_thread(blocked.wait, 3)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done() and supervisor._notify_lock(channel.worker_name).locked()
            supervisor._start_notify_beside_turn(host, channel, operation="input.available")
            assert supervisor._beside_notifies[channel.worker_name] is task, "no overlapping notification"
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled() and not supervisor._notify_lock(channel.worker_name).locked()
            assert set(field.pending_worker_mail_refs(channel.worker_name)) == {*quiet, actionable}
        finally:
            if release is not None:
                release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await supervisor.aclose()

    asyncio.run(scenario())


def test_card_replacement_during_scan_still_fences_coordinate_dispatch(queued_mail, monkeypatch):
    host, channel, field, _quiet, _actionable = queued_mail

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        scan = None
        release = None
        try:
            await supervisor.poll_once()
            session = supervisor._sessions[channel.worker_name]
            blocked, release, _reads = _block_scan(monkeypatch, "inbox_refs_not_in")
            scan = asyncio.create_task(supervisor._pending_for_wake(field, channel))
            assert await asyncio.to_thread(blocked.wait, 3)
            profiles_path = host.connection_hub_state_root / "profiles.json"
            profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
            for profile in profiles["profiles"]:
                if profile["name"] == channel.profile:
                    profile["access_id"] = "synthetic-replacement-card"
            profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
            queue = coordinate_queue.CoordinateQueue(host.field_root)
            request = submit_request(queue, channel)
            started = await asyncio.wait_for(supervisor.serve_coordinate_pass(), 2)
            assert channel.worker_name not in started
            assert session.client.calls == [], "scan concurrency must not authorize a stale session"
            assert queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"]) is None
        finally:
            if release is not None:
                release.set()
            if scan is not None:
                await scan
            await supervisor.aclose()

    asyncio.run(scenario())


def test_quiet_token_changes_and_new_operator_mail_remain_visible(queued_mail, monkeypatch):
    host, channel, field, quiet, actionable = queued_mail

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            await supervisor.poll_once()
            _notification_stubs(supervisor, monkeypatch)

            async def notify():
                return await relay.ProblemBoardRelaySupervisor._notify_available_input(supervisor, host, channel)

            await notify()
            assert not supervisor._quiet_classified[channel.worker_name][actionable]
            field.mark_backlog(channel.worker_name, reason="Synthetic mark")
            await notify()
            assert supervisor._quiet_classified[channel.worker_name][actionable]
            field.clear_backlog_mark(channel.worker_name)
            await notify()
            assert not supervisor._quiet_classified[channel.worker_name][actionable]
            # Actual acknowledgement changes the deferral token, without
            # deleting/settling mail. Ordinary check-in later clears it.
            field.check_in_worker_listener(channel.worker_name, acknowledge_wake=True,
                                           wake_id="wake_0000000000000000000000000000f456")
            await notify()
            assert supervisor._quiet_classified[channel.worker_name][actionable]
            pushed = []

            async def record_push(*_args, **kwargs):
                pushed.append(kwargs["message_refs"])
                return {"delivered": True}

            # After acknowledgement there is no queued wake to deduplicate;
            # a new operator reply is supposed to request a fresh native wake.
            monkeypatch.setattr(supervisor, "_notify_session", record_push)
            operator = field.send_mail(
                "", sender="control-plane", recipient=channel.worker_name, kind="reply",
                subject="Synthetic operator reply", body="Please continue.", idempotency_key="operator",
                payload={"expected_reaction": "acknowledge_only"},
                sender_identity={"kind": "user", "label": "Operator"}, admitted_operator_control=True,
            )["message_ref"]
            field.mark_backlog(channel.worker_name, reason="Operator remains current")
            await notify()
            assert supervisor._quiet_classified[channel.worker_name][operator] is False
            assert pushed[-1] == [operator], "operator mail is neither backlogged nor deferred nor quiet"
            field.clear_backlog_mark(channel.worker_name)
            field.check_in_worker_listener(channel.worker_name, inbox_checked=True)
            await notify()
            assert supervisor._quiet_classified[channel.worker_name][actionable] is False
            assert set(field.pending_worker_mail_refs(channel.worker_name)) == {*quiet, actionable, operator}
        finally:
            await supervisor.aclose()

    asyncio.run(scenario())

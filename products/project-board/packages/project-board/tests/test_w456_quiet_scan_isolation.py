"""Synthetic queued-wake scans cannot monopolize coordinate authority reads.

All mail and profiles belong to tmp_path; no live channel or runtime is used.
Cold and repeated scans block a real inbox JSON read while the production
coordinate server must claim, fence, dispatch and complete on the same channel.
This proves executor isolation, not live C7 or reduced filesystem work.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from project_board.client import coordinate_queue, relay
from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore
from relay_helpers import Attendance, submit_request, supervisor_with_fake_channels, two_channel_host


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "repeated"])
def test_queued_quiet_scan_does_not_hold_same_channel_coordinate(tmp_path, monkeypatch, warm):
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
    blocked, release = threading.Event(), threading.Event()
    reads: list[str] = []
    real_read = store_module.read_json

    def slow_inbox_read(path, *args, **kwargs):
        if path.parent.name == "inbox" and not release.is_set():
            reads.append(path.name)
            blocked.set()
            release.wait(10)
        return real_read(path, *args, **kwargs)

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        queue = coordinate_queue.CoordinateQueue(host.field_root)
        scan = server = None
        response = None
        elapsed = 0.0
        try:
            await supervisor.poll_once()
            if warm:
                previous, _listener, _full = await supervisor._pending_for_wake(field, channel)
                assert set(previous) == {*quiet, actionable}
            monkeypatch.setattr(store_module, "read_json", slow_inbox_read)
            scan = asyncio.create_task(supervisor._pending_for_wake(field, channel))
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
            release.set()
            if scan is not None:
                discovered, _listener, full = await scan
            if server is not None:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
            await supervisor.aclose()
        assert reads, "the scan must use real uncached JSON reads"
        assert response is not None and response["ok"] is True, (
            "a read-only quiet-mail scan held the same channel's coordinate executor")
        assert elapsed < 2, f"coordinate took {elapsed:.3f}s"
        assert not full and set(discovered) == {*quiet, actionable}

    asyncio.run(scenario())

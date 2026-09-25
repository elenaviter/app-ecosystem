"""A wake the relay holds for an agent's usage limit is a project event (W26 line 8).

Before this, the hold was one log line on the agent's host: the coordinator
saw "Rate limited" on the card with a fixed sentence under it and could not
tell whether a wake was waiting. The relay now publishes the hold, and its
end, as notification-path states the board already shows on the worker card.
"""

from __future__ import annotations

import asyncio

from project_board.client import relay
from project_board.client.store import SharedFieldStore
from relay_helpers import make_host, make_supervisor

PROJECT = "project-one"
RESET = "2026-09-25T09:00:00Z"


def _events(field, worker_name):
    rows = field.pull_outbox(relay_id="relay", worker_name=worker_name, project_ref=f"work:project:{PROJECT}", limit=20)
    return [row["payload"] for row in rows if row["kind"] == "event.publish"]


def _held_host(tmp_path, monkeypatch, *, attends=True):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="held-wake")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_alias="claude-ops",
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    field.listen_worker(identity.worker_name)
    field.create_project(project_id=PROJECT, title="Held wakes", goal="Show them.", owner="operator")
    if attends:
        field.sync_worker_attendances(identity.worker_name, [f"work:project:{PROJECT}"])
    supervisor = make_supervisor(host)

    async def no_reconciliation(*_args, **_kwargs):
        return None

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", no_reconciliation)
    monkeypatch.setattr(SharedFieldStore, "pending_worker_mail_refs", lambda self, name: ["work:message:one", "work:message:two"])
    limit = {"until": RESET}
    monkeypatch.setattr(relay, "wake_deferred_until", lambda state, now: limit["until"])
    return host, channel, field, supervisor, limit


def test_a_held_wake_is_published_once_per_reset_and_its_end_once(tmp_path, monkeypatch):
    host, channel, field, supervisor, limit = _held_host(tmp_path, monkeypatch)

    held = asyncio.run(supervisor._notify_available_input(host, channel))  # noqa: SLF001 - the path under test
    asyncio.run(supervisor._notify_available_input(host, channel))  # noqa: SLF001 - the same reset, no second event

    assert held["wake_deferred"] is True
    published = _events(field, channel.worker_name)
    [event] = published
    assert event["kind"] == "worker.notification_path"
    assert event["metadata"]["state"] == "wake_deferred"
    assert event["metadata"]["until"] == RESET
    assert event["metadata"]["pending_messages"] == 2
    assert event["metadata"]["incident_kind"] == "usage_limit"
    assert "claude-ops: wake held until 2026-09-25T09:00:00Z" in event["summary"]

    # The reset passes: the wake goes out and the card's state closes.
    limit["until"] = ""
    monkeypatch.setattr(field.__class__, "worker_listener_session", lambda self, name: {"state": "listening"})
    asyncio.run(supervisor._notify_available_input(host, channel))  # noqa: SLF001

    # A pull leases what it returns, so this one holds only what is new.
    published += _events(field, channel.worker_name)
    states = [row["metadata"]["state"] for row in published]
    assert states == ["wake_deferred", "wake_resumed"]


def test_a_worker_in_no_project_holds_the_wake_without_an_event(tmp_path, monkeypatch):
    host, channel, field, supervisor, _limit = _held_host(tmp_path, monkeypatch, attends=False)

    held = asyncio.run(supervisor._notify_available_input(host, channel))  # noqa: SLF001

    assert held["wake_deferred"] is True
    assert _events(field, channel.worker_name) == []

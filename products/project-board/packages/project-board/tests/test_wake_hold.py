"""A wake the relay holds for a limited agent is visible on every card (W334).

The earlier attempt (app-ecosystem#128) published the hold as events and its
review found five faults: a process-local lifecycle, one project only, a
resume sent too early, a slot shared with path health, and ``since`` holding
the reset time. Here the hold is state in the worker's own record, written by
the same decision that withholds the wake and read by every heartbeat.
"""

from __future__ import annotations

import asyncio
from typing import Any

from project_board.client import relay
from project_board.client.store import SharedFieldStore
from relay_helpers import make_host, make_supervisor

LIMITED = {"kind": "rate_limited", "resets_at": "2999-01-01T00:00:00Z", "reached": "primary", "windows": []}


def _field(tmp_path):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="wake-hold")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    return host, identity, channel, field


def test_the_hold_is_kept_in_the_record_and_since_is_when_it_began(tmp_path):
    host, identity, _channel, field = _field(tmp_path)
    first = field.record_wake_hold(identity.worker_name, until="2999-01-01T00:00:00Z", pending=2)
    assert first["until"] == "2999-01-01T00:00:00Z" and first["pending"] == 2
    assert first["since"] < first["until"], "since is when the hold began, until the reset"
    # Later cycles keep since; the pending count follows the mail.
    again = field.record_wake_hold(identity.worker_name, until="2999-01-01T00:00:00Z", pending=3)
    assert again["since"] == first["since"] and again["pending"] == 3
    # A relay restart reads the same record.
    assert SharedFieldStore(host.field_root).wake_hold(identity.worker_name) == again
    field.clear_wake_hold(identity.worker_name)
    assert field.wake_hold(identity.worker_name) == {}
    # A new hold starts a new since.
    assert field.record_wake_hold(identity.worker_name, until="2999-01-02T00:00:00Z", pending=1)["since"] >= first["since"]


class _Scripted:
    """What the field and queue say in one delivery cycle."""

    listener: dict[str, Any] | None = {"state": "attached", "subscription": {}}
    pending: list[str] = ["work:mail:one"]
    limit: dict[str, Any] = LIMITED
    reconciliation: dict[str, Any] | None = None


def _decide(monkeypatch, host, channel, script: type[_Scripted]) -> tuple[dict[str, Any] | None, list[str]]:
    woken: list[str] = []

    class Field(SharedFieldStore):
        def worker_listener_session(self, worker_name):
            return dict(script.listener) if script.listener is not None else None

        def pending_worker_mail_refs(self, worker_name):
            return list(script.pending)

    monkeypatch.setattr(relay, "SharedFieldStore", Field)
    monkeypatch.setattr(relay, "session_with_limit_state", lambda listener, **_: {**listener, "limit_state": script.limit})
    supervisor = make_supervisor(host)

    async def reconcile(*_args, **_kwargs):
        return script.reconciliation

    async def notify(*_args, **_kwargs):
        woken.append("wake")
        return {"woken": True}

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_notify_session", notify)
    result = asyncio.run(supervisor._notify_available_input(host, channel))
    return result, woken


def test_a_held_wake_is_recorded_kept_while_reconciliation_withholds_and_cleared_when_eligible(tmp_path, monkeypatch):
    host, identity, channel, field = _field(tmp_path)

    class Held(_Scripted):
        pass

    result, woken = _decide(monkeypatch, host, channel, Held)
    assert result["wake_deferred"] is True and not woken
    hold = field.wake_hold(identity.worker_name)
    assert hold["until"] == LIMITED["resets_at"] and hold["pending"] == 1

    # The reset passed, but reconciliation still withholds the wake: the hold stays.
    class Withheld(_Scripted):
        limit = {"kind": "ok"}
        pending = ["work:mail:one", "work:mail:two"]
        listener = {"state": "attached", "subscription": {"outstanding_wake_id": "wake_2"}}
        reconciliation = {"reconciled": False, "reason": "codex_app_server_not_started"}

    _decide(monkeypatch, host, channel, Withheld)
    kept = field.wake_hold(identity.worker_name)
    assert kept["since"] == hold["since"] and kept["pending"] == 2, "still withheld: still held"

    # Eligible again: the hold ends where the wake proceeds.
    class Eligible(_Scripted):
        limit = {"kind": "ok"}

    _decide(monkeypatch, host, channel, Eligible)
    assert field.wake_hold(identity.worker_name) == {}


def test_a_hold_whose_mail_is_gone_clears(tmp_path, monkeypatch):
    host, identity, channel, field = _field(tmp_path)
    _decide(monkeypatch, host, channel, _Scripted)
    assert field.wake_hold(identity.worker_name)

    class Empty(_Scripted):
        pending: list[str] = []

    _decide(monkeypatch, host, channel, Empty)
    assert field.wake_hold(identity.worker_name) == {}


def test_every_heartbeat_states_the_hold_and_a_stale_one_never_shows():
    hold = {"since": "2026-09-26T03:00:00Z", "until": "2026-09-26T05:00:00Z", "pending": 2}
    row = relay.session_with_wake_hold({"session_id": "s"}, hold=hold, pending=3)
    assert row["wake_hold"] == {"since": "2026-09-26T03:00:00Z", "until": "2026-09-26T05:00:00Z", "pending": 3}
    # The mail was taken before the delivery loop ran again: nothing is held.
    assert relay.session_with_wake_hold({"session_id": "s"}, hold=hold, pending=0)["wake_hold"] == {}
    assert relay.session_with_wake_hold({"session_id": "s"}, hold={}, pending=5)["wake_hold"] == {}
    # It rides the projected session (every attended project's heartbeat
    # carries agent_sessions), beside, never inside, the subscription.
    projected = relay._heartbeat_session_projection({**row, "subscription": {"state": "dead"}})
    assert projected["wake_hold"]["pending"] == 3
    assert "wake_hold" not in projected.get("subscription", {})

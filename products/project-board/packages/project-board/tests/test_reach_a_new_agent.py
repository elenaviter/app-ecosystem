"""A new agent can be reached before it attends a project (W304 finding 24 and decision 3).

Operator, 2026-09-26: before an agent attends any project, anyone who knows
its id may mail it, to steer it; and an agent is told when it is approved,
instead of waiting for someone to type into its session.

- Mail without a project always goes to the board, which delivers it only to
  an agent that attends no project, addressed by its exact stable name. The
  board's direct control lands in the recipient's direct mailbox; no local
  send can write a worker's mail there.
- Approval wakes the session where the runtime lets it be woken from outside
  (Codex, through its queue). Claude Code has no outside door: its own running
  watch reports the approval, once.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from project_board.client import relay, session_delivery
from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore
from project_board.client.worker_watch import worker_watch_events
from project_board.contract.errors import DomainError
from relay_helpers import make_host

SENDER = "claude-code-0107f9f8-ef3a-4272-ad87-658341dc1d5a"
NEW_AGENT = "claude-code-11111111-2222-4333-8444-555555555555"


def _field(tmp_path, *, control_plane_state="published"):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="reach-new-agent")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state=control_plane_state,
    )
    return host, identity, channel, field


def test_mail_without_a_project_always_goes_to_the_board(tmp_path, monkeypatch):
    from argparse import Namespace

    from project_board.client import cli

    host, identity, _channel, field = _field(tmp_path)
    queued = []
    monkeypatch.setattr(
        SharedFieldStore,
        "enqueue_remote_mail",
        lambda self, project_id, **values: queued.append((project_id, values)) or {"outbox_id": "outbox_one"},
    )
    monkeypatch.setattr(cli, "_await_outbox_outcome", lambda *_args, **_kwargs: {"state": "sent"}, raising=False)
    # A session on this very host too: no local path bypasses the board's rule.
    for recipient in (identity.worker_name, NEW_AGENT):
        values = {
            "command": "worker", "worker_command": "send",
            "runtime_kind": identity.runtime_kind, "runtime_session_id": identity.runtime_session_id,
            "config": str(host.path), "project_ref": None, "recipient": recipient, "route": "auto",
            "kind": "request", "subject": "Steer", "body": "Wait for your owner.", "body_file": None,
            "payload_file": None, "work_ref": None, "correlation_id": None, "reply_to": None,
            "idempotency_key": f"steer-{recipient}", "attach": None,
        }
        try:
            cli._worker_command(Namespace(**values))
        except DomainError as exc:  # anything after queueing is not under test
            assert exc.code not in {"field_mail_route_mismatch", "field_project_context_required"}, exc
    assert [(project, values["recipient"]) for project, values in queued] == [
        ("", identity.worker_name),
        ("", NEW_AGENT),
    ]


def test_a_worker_sends_a_request_reply_or_ping_without_a_project(tmp_path):
    _host, identity, _channel, field = _field(tmp_path)

    for kind in ("request", "reply", "ping"):
        field.enqueue_remote_mail(
            "",
            sender=identity.worker_name,
            recipient=NEW_AGENT,
            kind=kind,
            subject=f"Steer {kind}",
            body="Join the project when the operator links you.",
            idempotency_key=f"steer-{kind}",
        )
    with pytest.raises(DomainError) as refused:
        field.enqueue_remote_mail(
            "",
            sender=identity.worker_name,
            recipient=NEW_AGENT,
            kind="question",
            subject="A question",
            body="Not a direct kind.",
            idempotency_key="steer-question",
        )
    assert refused.value.code == "field_direct_mail_kind_invalid"
    assert refused.value.details["allowed"] == ["ping", "reply", "request"]


def test_the_boards_direct_mail_reaches_the_new_agents_direct_mailbox(tmp_path):
    _host, identity, _channel, field = _field(tmp_path)
    field.listen_worker(identity.worker_name)
    mail = {
        "mail": {
            "kind": "request",
            "subject": "Welcome",
            "body": "Your owner will link you to a project shortly.",
            "source_message_ref": "work:mail:20260926T180000Z:mail_one:welcome",
            "payload": {},
            "work_ref": "",
            "identity_ref": "",
        }
    }
    field.materialize_control(
        {
            "ref": "work:control:20260926T180000Z:command_one:welcome",
            "kind": "mail",
            "subject": "Welcome",
            "project_ref": "",
            "recipient": identity.worker_name,
            "sender": SENDER,
            "payload": mail,
            "payload_hash": content_hash(mail),
        }
    )

    [received] = field.pull_mail("", worker_name=identity.worker_name, lease_owner="session")
    assert received["sender"] == SENDER
    assert received["kind"] == "request"
    assert received["subject"] == "Welcome"


def test_no_local_send_writes_a_workers_mail_into_a_direct_mailbox(tmp_path):
    _host, identity, _channel, field = _field(tmp_path)

    with pytest.raises(DomainError) as refused:
        field.send_mail(
            "",
            sender=SENDER,
            recipient=identity.worker_name,
            kind="request",
            subject="Bypass",
            body="A local write the board never saw.",
            idempotency_key="bypass-1",
        )
    assert refused.value.code == "field_project_context_required"
    assert "goes through the board" in str(refused.value)


def test_approval_queues_a_codex_wake_and_leaves_claude_code_to_its_watch(tmp_path, monkeypatch):
    host, _identity, channel, _field_store = _field(tmp_path)
    calls = []

    class Completed:
        returncode = 0
        stdout = ""

    def queue(command, **_kwargs):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(session_delivery, "_codex_executable", lambda: Path("/usr/bin/codex"))
    monkeypatch.setattr(session_delivery.subprocess, "run", queue)

    codex = session_delivery.notify_agent_session(channel, event_kind="control_plane.connected", wake_id="wake_one")
    assert codex["delivered"] is True
    [command] = calls
    assert command[1:4] == ["queue", "--thread", channel.runtime_session_id]
    message = command[command.index("--message") + 1]
    assert "Problem Board authorized this already-running session" in message
    assert "pb worker receive --wake-id wake_one" in message

    import dataclasses

    claude = session_delivery.notify_agent_session(
        dataclasses.replace(channel, runtime_kind="claude-code"),
        event_kind="control_plane.connected",
    )
    assert claude["delivered"] is False
    assert claude["reason"] == "runtime_has_no_supported_local_queue"
    assert len(calls) == 1

    # Any other status event still wakes nobody.
    other = session_delivery.notify_agent_session(channel, event_kind="control_plane.state_changed")
    assert other["reason"] == "status_event_does_not_wake_model"
    assert relay.WAKE_EVENT_KINDS == session_delivery.WAKE_EVENT_KINDS


def test_a_running_watch_reports_the_approval_exactly_once(tmp_path):
    _host, identity, _channel, field = _field(tmp_path, control_plane_state="pending_authorization")
    field.listen_worker(identity.worker_name)
    events: list[dict] = []
    generator = worker_watch_events(
        field, worker_name=identity.worker_name, check_interval_seconds=5, coalesce_seconds=0
    )
    threading.Thread(target=lambda: events.extend(generator), daemon=True).start()
    time.sleep(1.0)
    assert events == []  # nothing to report while the Card waits for approval

    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and not events:
        time.sleep(0.1)
    time.sleep(11)  # two more probe intervals: the same signal is not repeated

    assert len(events) == 1, events
    [signal] = events[0]["signals"]
    assert signal["kind"] == "control_plane.connected"
    assert signal["state"] == "published"

"""A notice that needs no action wakes no session; it stays pending (W563, Q2).

Why: every pending message woke the coordinator, including Done and Cancelled
assignment notices its producer had marked `expected_reaction:
acknowledge_only`. Those still arrive, counted, with the next receive; only
mail that asks for action, and every operator message, starts a turn.
"""

from __future__ import annotations

import asyncio

from project_board.client.worker_watch import _availability
from relay_helpers import make_supervisor
from test_w456_notify_off_loop import _codex_channels, _with_session_stubs


PROJECT = "quiet-project"


def _attend(field, worker: str) -> None:
    field.create_project(project_id=PROJECT, title="Quiet", goal="Notices that need no action.", owner="operator")
    field.sync_worker_attendances(worker, [f"work:project:{PROJECT}"])


def _notice(field, worker: str, number: int) -> dict:
    return field.send_mail(
        PROJECT, sender="control-plane", recipient=worker, kind="update", subject=f"W{number} is Done",
        body="Information only.", payload={"expected_reaction": "acknowledge_only"},
        idempotency_key=f"notice-{number}",
    )


def test_only_acknowledge_only_notices_are_quiet(tmp_path):
    host, field, (channel,) = _codex_channels(tmp_path, 1, first_mail=0)
    _attend(field, channel.worker_name)
    quiet = _notice(field, channel.worker_name, 1)
    action = field.send_mail(PROJECT, sender="control-plane", recipient=channel.worker_name, kind="update",
                             subject="Deploy request", body="Please deploy.", idempotency_key="action")

    assert field.quiet_mail_refs(channel.worker_name) == {quiet["message_ref"]}
    signature, event = _availability(
        {"pending_refs": [quiet["message_ref"], action["message_ref"]], "pending_count": 2},
        frozenset({quiet["message_ref"]}),
    )
    assert signature == (action["message_ref"],)
    assert event["pending_count"] == 2 and event["quiet_pending_count"] == 1
    quiet_only, _event = _availability({"pending_refs": [quiet["message_ref"]], "pending_count": 1},
                                       frozenset({quiet["message_ref"]}))
    assert quiet_only == ()


def test_the_relay_does_not_wake_for_quiet_notices_alone(tmp_path):
    host, field, (channel,) = _codex_channels(tmp_path, 1, first_mail=0)
    pushed: list[str] = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)
    _attend(field, channel.worker_name)
    notice = _notice(field, channel.worker_name, 2)

    asyncio.run(supervisor._notify_available_input(host, channel))
    assert pushed == []
    assert field.pending_worker_mail_refs(channel.worker_name) == [notice["message_ref"]], "the notice stays pending"

    field.send_mail(PROJECT, sender="control-plane", recipient=channel.worker_name, kind="request",
                    subject="Review W9", body="Please review.", idempotency_key="review")
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert pushed == [channel.worker_name]

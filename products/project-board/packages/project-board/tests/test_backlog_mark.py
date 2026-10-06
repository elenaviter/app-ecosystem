"""A backlog mark lets current mail through ahead of history (W563).

Why: on 2026-10-06 the coordinator's native wakes each leased the five oldest
bodies of a long backlog (5 October 12:44–13:12) while the current window
control waited behind them, and after three selective receives the client
required an ordinary receive, which leased history again before the ALL CLEAR
could be selected (coordinator, 00:35Z and 00:41Z). The mark names the exact
messages pending when it is set. The ordinary receive and native wakes then
deliver current mail; the backlog stays pending, counted on every receive,
and is received with --backlog. Nothing is settled or retired by the mark.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.io import content_hash
from project_board.client.render import render_envelope
from project_board.client.session import pull_worker_input
from project_board.client.store import SELECTIVE_RECEIVE_BUDGET, SharedFieldStore
from project_board.client.worker_watch import _availability
from project_board.contract.errors import DomainError


PROJECT = "backlog-project"
WORKER = "claude-main"
SENDER = "codex-app"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="backlog-test")
    for name in (WORKER, SENDER):
        store.register_worker(worker_name=name, runtime_kind="codex", capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT, title="Backlog", goal="Current mail first.", owner="operator")
    store.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    store.listen_worker(WORKER)
    return store


def _send(field: SharedFieldStore, key: str, *, kind: str = "update", subject: str = "", correlation_id: str = "") -> dict:
    return field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind=kind, subject=subject or key, body=f"BODY {key}",
        idempotency_key=key, correlation_id=correlation_id,
    )


def _history(field: SharedFieldStore, count: int = 40) -> list[dict]:
    rows = []
    for number in range(count):
        kind = "request" if number in (3, 17) else "decision" if number == 30 else "update"
        rows.append(_send(field, f"old-{number}", kind=kind, subject=f"Old {number}"))
    return rows


def _subjects(received: dict) -> list[str]:
    return [item["message"]["subject"] for item in received["items"]]


def test_after_a_mark_the_ordinary_receive_delivers_the_current_control_not_history(field):
    _history(field)
    mark = field.mark_backlog(WORKER, reason="W563: 40 messages from yesterday")
    assert (mark["count"], mark["unresolved_count"]) == (40, 3)
    all_clear = _send(field, "all-clear", subject="ALL CLEAR: window w-13", correlation_id="window-w-13")

    received = pull_worker_input(field, worker_name=WORKER, limit=5)

    assert _subjects(received) == ["ALL CLEAR: window w-13"]
    assert received["items"][0]["message"]["message_ref"] == all_clear["message_ref"]
    assert received["delivery"]["remaining_count"] == 0
    backlog = received["backlog"]
    assert (backlog["pending_count"], backlog["unresolved_count"], backlog["marked_count"]) == (40, 3, 40)
    assert backlog["oldest_ref"] and not backlog["received_now"]
    text = render_envelope({"ok": True, "result": received})
    assert "backlog: pending 40 · requests, decisions and questions 3" in text
    assert "pb worker receive --backlog" in text
    # Nothing was settled or leased from the backlog.
    assert len([header for header in field.pending_mail_headers(WORKER) if header["backlog"]]) == 40


def test_the_backlog_is_received_oldest_first_only_on_request(field):
    _history(field, 8)
    field.mark_backlog(WORKER, reason="set aside")
    _send(field, "new-1", subject="New 1")

    taken = pull_worker_input(field, worker_name=WORKER, limit=3, backlog=True)

    assert _subjects(taken) == ["Old 0", "Old 1", "Old 2"]
    assert taken["backlog"]["received_now"] is True
    assert taken["backlog"]["pending_count"] == 5
    assert taken["delivery"]["remaining_count"] == 5
    assert "this batch is backlog" in render_envelope({"ok": True, "result": taken})
    # The current message was not taken by the backlog receive.
    assert _subjects(pull_worker_input(field, worker_name=WORKER, limit=5)) == ["New 1"]


def test_mail_after_the_mark_is_current_and_operator_mail_is_never_backlog(field):
    field.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    _history(field, 3)
    payload = {"body": "Approved.", "correlation_id": "approval"}
    operator = field.materialize_control({
        "ref": "work:control:20261006T005000Z:command_approval:w563-approval",
        "project_ref": f"work:project:{PROJECT}", "recipient": WORKER, "kind": "reply",
        "subject": "Operator approval", "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })
    mark = field.mark_backlog(WORKER, reason="set aside")
    assert mark["count"] == 3, "operator mail pending at the mark is not set aside"
    _send(field, "after", subject="After the mark")

    received = pull_worker_input(field, worker_name=WORKER, limit=5)

    assert _subjects(received) == ["Operator approval", "After the mark"]
    assert received["items"][0]["message"]["message_ref"] == operator["message_ref"]


def test_three_selective_receives_then_the_ordinary_receive_brings_current_mail(field):
    # The selective budget stays: after it, an ordinary receive is due. With a
    # mark that receive delivers current mail, so the ALL CLEAR is not held
    # behind history (coordinator, 00:41Z).
    _history(field, 20)
    field.mark_backlog(WORKER, reason="set aside")
    picks = [_send(field, f"pick-{number}", subject=f"Pick {number}") for number in range(SELECTIVE_RECEIVE_BUDGET)]
    for pick in picks:
        pull_worker_input(field, worker_name=WORKER, message_ref=pick["message_ref"])
    all_clear = _send(field, "all-clear", subject="ALL CLEAR")
    with pytest.raises(DomainError) as refused:
        pull_worker_input(field, worker_name=WORKER, message_ref=all_clear["message_ref"])
    assert refused.value.code == "field_mail_general_receive_due"

    ordinary = pull_worker_input(field, worker_name=WORKER, limit=5)

    assert _subjects(ordinary) == ["ALL CLEAR"]
    assert ordinary["backlog"]["pending_count"] == 20


def test_backlog_wakes_no_session_until_the_mark_is_cleared(field):
    rows = _history(field, 4)
    refs = [row["message_ref"] for row in rows]
    assert field.quiet_mail_refs(WORKER, refs=refs) == set()

    field.mark_backlog(WORKER, reason="set aside")
    assert field.quiet_mail_refs(WORKER, refs=refs) == set(refs)
    assert field.quiet_mail_refs(WORKER) == set(refs)
    signature, event = _availability({"pending_refs": refs, "pending_count": 4}, frozenset(refs))
    assert signature == () and event["quiet_pending_count"] == 4

    current = _send(field, "current")
    assert field.quiet_mail_refs(WORKER, refs=[current["message_ref"]]) == set()

    ended = field.clear_backlog_mark(WORKER)
    assert ended["count"] == 4
    assert field.quiet_mail_refs(WORKER, refs=refs) == set()
    assert field.backlog_mark(WORKER) == {}
    # History keeps the ended mark; ordinary receive is oldest first again.
    assert _subjects(pull_worker_input(field, worker_name=WORKER, limit=1)) == ["Old 0"]


def test_backlog_receive_needs_a_mark_and_takes_no_wake_or_selection(field):
    _history(field, 2)
    with pytest.raises(DomainError) as unmarked:
        pull_worker_input(field, worker_name=WORKER, backlog=True)
    assert unmarked.value.code == "field_mail_backlog_not_marked"
    field.mark_backlog(WORKER, reason="set aside")
    with pytest.raises(DomainError) as woken:
        pull_worker_input(field, worker_name=WORKER, backlog=True, wake_id="wake_1")
    assert woken.value.code == "field_mail_backlog_wake_invalid"
    with pytest.raises(DomainError) as reason:
        field.mark_backlog(WORKER, reason="")
    assert reason.value.code


def test_the_inbox_flags_backlog_headers(field):
    _history(field, 3)
    field.mark_backlog(WORKER, reason="set aside")
    _send(field, "fresh", kind="question", subject="Fresh question")
    args = types.SimpleNamespace(limit=20, kind=[], sender="", since="")

    inventory = cli._worker_inbox(field, types.SimpleNamespace(worker_name=WORKER), args)
    text = render_envelope({"ok": True, "result": inventory})

    assert inventory["pending"] == 4 and inventory["backlog"] == 3
    assert "backlog: 3 of the pending are set aside" in text
    assert "--- action · " in text and "Fresh question" in text
    assert text.count("· backlog ·") == 3


def test_a_new_mark_replaces_the_old_one_and_keeps_it_as_history(field):
    _history(field, 2)
    first = field.mark_backlog(WORKER, reason="first")
    _send(field, "between")
    second = field.mark_backlog(WORKER, reason="second")

    assert second["count"] == 3 and second["mark_id"] != first["mark_id"]
    stored = field._backlog_path(WORKER).read_text(encoding="utf-8")
    assert first["mark_id"] in stored and '"reason": "first"' in stored


def test_the_parser_offers_the_mark_and_the_backlog_receive():
    parser = cli.build_parser()
    marked = parser.parse_args(["worker", "backlog-mark", "--reason", "W563 relief"])
    assert (marked.worker_command, marked.reason, marked.clear) == ("backlog-mark", "W563 relief", False)
    assert parser.parse_args(["worker", "backlog-mark", "--clear"]).clear is True
    assert parser.parse_args(["worker", "receive", "--backlog"]).backlog is True


def test_window_control_is_received_by_correlation_without_leasing_unrelated_work(field):
    # Root, 6 October 08:44 UTC: after three window-only selective reads, the
    # exact receive of ALL CLEAR was refused (general receive due), and the
    # ordinary fallback forced six unrelated bodies to reach the release.
    from project_board.client.store import CONTROL_SELECTION_BUDGET

    unrelated = [_send(field, f"unrelated-{n}", kind="request", subject=f"Unrelated {n}") for n in range(6)]
    window = "client-063-window-devmain"
    sequence = ["READY requested", "Renew READY now", "START", "ALL CLEAR"]
    taken = []
    for subject in sequence:
        _send(field, f"ctl-{subject}", kind="request", subject=subject, correlation_id=window)
        got = pull_worker_input(field, worker_name=WORKER, correlation_id=window, sender=SENDER)
        taken.extend(_subjects(got))
        assert got["selection"]["control_selections_remaining"] == CONTROL_SELECTION_BUDGET - len(taken)

    assert taken == sequence, "every control message, in order, and nothing else"
    pending = {header["message_ref"] for header in field.pending_mail_headers(WORKER)}
    assert {row["message_ref"] for row in unrelated} <= pending, "no unrelated work was leased"
    # The general budget is untouched, so ordinary fairness is unchanged.
    listener = field.worker_listener_session(WORKER)
    assert listener["selective_receives_since_general"] == 0 and not listener["general_receive_due"]
    # The ordinary receive still delivers the unrelated mail, oldest first,
    # and resets the control budget.
    ordinary = pull_worker_input(field, worker_name=WORKER, limit=10)
    assert _subjects(ordinary) == [f"Unrelated {n}" for n in range(6)]
    assert field.worker_listener_session(WORKER)["control_selections_used"] == 0


def test_the_control_budget_is_bounded_and_one_correlation_at_a_time(field):
    from project_board.client.store import CONTROL_SELECTION_BUDGET

    for pick in [_send(field, f"pick-{n}", subject=f"Pick {n}") for n in range(SELECTIVE_RECEIVE_BUDGET)]:
        pull_worker_input(field, worker_name=WORKER, message_ref=pick["message_ref"])
    for number in range(CONTROL_SELECTION_BUDGET):
        _send(field, f"w1-{number}", subject=f"W1 {number}", correlation_id="window-1")
        pull_worker_input(field, worker_name=WORKER, correlation_id="window-1", sender=SENDER)

    _send(field, "w1-late", subject="W1 late", correlation_id="window-1")
    with pytest.raises(DomainError) as spent:
        pull_worker_input(field, worker_name=WORKER, correlation_id="window-1", sender=SENDER)
    assert spent.value.code == "field_mail_general_receive_due"
    _send(field, "w2", subject="W2", correlation_id="window-2")
    with pytest.raises(DomainError) as other:
        pull_worker_input(field, worker_name=WORKER, correlation_id="window-2", sender=SENDER)
    assert other.value.code == "field_mail_general_receive_due", "a second correlation uses the general budget"


def test_a_selected_receive_never_reports_an_empty_backlog(field):
    # Root, 08:44 UTC: a selected receive printed "backlog: pending 0" and
    # advised clearing the mark while 300 marked messages were pending.
    _history(field, 12)
    field.mark_backlog(WORKER, reason="set aside")
    control = _send(field, "ctl", subject="ALL CLEAR", correlation_id="window-x")

    got = pull_worker_input(field, worker_name=WORKER, message_ref=control["message_ref"])

    assert got["backlog"]["counted"] is False
    assert "pending_count" not in got["backlog"]
    text = render_envelope({"ok": True, "result": got})
    assert "backlog: not counted by this selected receive · marked 12" in text
    assert "The backlog is empty" not in text and "backlog: pending 0" not in text
    assert len([header for header in field.pending_mail_headers(WORKER) if header["backlog"]]) == 12

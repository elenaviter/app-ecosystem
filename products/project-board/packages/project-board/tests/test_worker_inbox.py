"""pb worker inbox lists what is waiting, action first, without bodies (W563).

Why: a coordinator with 374 pending messages received last evening's updates
one by one, bodies and all, while a new decision thread waited behind them:
ordinary receive is oldest first, and a selective receive needs a ref the
worker does not know yet. The inventory names every pending message by its
header so the worker can receive the one it needs by ref. It leases nothing,
and ordinary receive still delivers the oldest mail first.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.render import render_envelope
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore


PROJECT = "inbox-project"
WORKER = "claude-main"
SENDER = "codex-app"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="inbox-test")
    for name in (WORKER, SENDER):
        store.register_worker(worker_name=name, runtime_kind="codex", capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT, title="Inbox", goal="Find new decisions behind a backlog.", owner="operator")
    store.sync_worker_attendances(WORKER, [f"work:project:{PROJECT}"])
    store.listen_worker(WORKER)
    return store


def _send(field: SharedFieldStore, number: int, *, kind: str, subject: str, body: str) -> dict:
    return field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind=kind, subject=subject, body=body,
        idempotency_key=f"mail-{number}",
    )


def _inbox(field: SharedFieldStore, **options) -> dict:
    args = types.SimpleNamespace(limit=20, kind=[], sender="", since="")
    for key, value in options.items():
        setattr(args, key, value)
    return cli._worker_inbox(field, types.SimpleNamespace(worker_name=WORKER), args)


def test_a_new_decision_behind_a_350_message_backlog_is_listed_first_and_received_by_ref(field):
    for number in range(350):
        _send(field, number, kind="update", subject=f"Old status {number}", body=f"OLD_BODY_{number} " * 20)
    urgent = _send(field, 999, kind="question", subject="Approve the release window?", body="URGENT_BODY")

    inventory = _inbox(field, limit=5)
    text = render_envelope({"ok": True, "result": inventory})

    assert inventory["pending"] == 351 and inventory["returned"] == 5
    assert inventory["classes"] == {"operator": 0, "action": 1, "information": 350}
    assert inventory["headers"][0]["message_ref"] == urgent["message_ref"]
    assert f"message_ref: {urgent['message_ref']}" in text
    assert "--- action · " in text and "question · from codex-app · Approve the release window?" in text
    # Headers only: no body or payload ever, and nothing was leased.
    assert "OLD_BODY" not in text and "URGENT_BODY" not in text
    assert len(text.encode("utf-8")) < 3_000
    assert field.list_worker_mail_leases(WORKER, lease_owner=WORKER, cursor="", limit=20)["total"] == 0

    selected = pull_worker_input(field, worker_name=WORKER, message_ref=urgent["message_ref"])
    assert [item["message"]["message_ref"] for item in selected["items"]] == [urgent["message_ref"]]
    # Ordinary receive is unchanged: it still delivers the oldest mail first.
    ordinary = pull_worker_input(field, worker_name=WORKER, limit=1)
    assert ordinary["items"][0]["message"]["subject"] == "Old status 0"


def test_filters_and_the_newest_first_order_within_a_class(field):
    _send(field, 1, kind="update", subject="first update", body="b")
    _send(field, 2, kind="request", subject="older request", body="b")
    _send(field, 3, kind="request", subject="newer request", body="b")

    requests = _inbox(field, kind=["request"])
    assert [header["subject"] for header in requests["headers"]] == ["newer request", "older request"]
    assert requests["pending_by_kind"] == {"request": 2, "update": 1}
    assert _inbox(field, sender="someone-else")["matched"] == 0
    with pytest.raises(Exception):
        _inbox(field, limit=101)


def test_a_held_session_learns_the_all_clear_without_touching_its_backlog(field):
    # W563, operator order 2026-10-05 19:32Z: a worker held for a host client
    # window stopped all PB calls, so it never received the window's ALL CLEAR
    # and stayed held. The hold allows window control: the session finds the
    # window's message by header and receives exactly it; its backlog stays
    # pending and untouched until the hold ends.
    for number in range(40):
        _send(field, number, kind="update", subject=f"Old status {number}", body="history")
    all_clear = field.send_mail(
        PROJECT, sender="codex-main", recipient=WORKER, kind="update",
        subject="ALL CLEAR: window w-1", body="Resume now.", correlation_id="window-w-1",
        idempotency_key="all-clear-w-1",
    )

    window = [header for header in _inbox(field, limit=100)["headers"] if header["correlation_id"] == "window-w-1"]
    assert [header["message_ref"] for header in window] == [all_clear["message_ref"]]

    taken = pull_worker_input(field, worker_name=WORKER, message_ref=all_clear["message_ref"])
    assert [item["message"]["subject"] for item in taken["items"]] == ["ALL CLEAR: window w-1"]
    assert _inbox(field, limit=100)["pending"] == 40, "the held session's backlog stays pending"


def test_the_procedure_lets_a_held_session_receive_its_window_control():
    from project_board.client import procedures

    text = " ".join(
        (procedures.source_package_path() / "references" / "runtime-actions.md").read_text(encoding="utf-8").split()
    )
    assert "**Window control stays allowed while held**" in text
    assert "`pb worker inbox`, `pb worker receive`" in text and "`pb worker settle`" in text
    assert "receiving a message is never the end of the hold" in text
    assert "issue no commands" not in text


def test_a_held_session_acknowledges_a_native_wake_without_receiving_its_backlog(field):
    # W563, Root 2026-10-05 22:19 UTC: while held for a host window, two native
    # wakes forced ordinary receives that delivered old W502/W538 notices, and
    # `receive --wake-id` with a window selection was refused
    # (field_mail_selection_wake_invalid). wake-ack records the wake as handled
    # and leases nothing; the window mail is found by header and taken by ref.
    for number in range(40):
        _send(field, number, kind="update", subject=f"Old status {number}", body="history")
    all_clear = field.send_mail(
        PROJECT, sender="codex-main", recipient=WORKER, kind="update",
        subject="ALL CLEAR: window w-2", body="Resume now.", correlation_id="window-w-2",
        idempotency_key="all-clear-w-2",
    )
    field.prepare_worker_session_wake(WORKER, message_refs=[all_clear["message_ref"]], wake_id="wake_hold_1")

    acknowledged = field.acknowledge_worker_wake(WORKER, wake_id="wake_hold_1")

    assert acknowledged["wake"] == {"id": "wake_hold_1", "state": "acknowledged", "expected_id": ""}
    subscription = field.worker_listener_session(WORKER)["subscription"]
    assert "outstanding_wake_id" not in subscription
    assert _inbox(field, limit=100)["pending"] == 41, "nothing was leased or delivered"
    listener = field.worker_listener_session(WORKER)
    assert not listener.get("general_receive_due") and not listener.get("selective_receives_since_general")
    taken = pull_worker_input(field, worker_name=WORKER, message_ref=all_clear["message_ref"])
    assert [item["message"]["subject"] for item in taken["items"]] == ["ALL CLEAR: window w-2"]
    assert _inbox(field, limit=100)["pending"] == 40

    again = field.acknowledge_worker_wake(WORKER, wake_id="wake_hold_1")
    assert again["wake"]["state"] == "already_acknowledged"


def test_the_procedure_lets_a_held_session_acknowledge_a_wake():
    from project_board.client import procedures

    text = " ".join(
        (procedures.source_package_path() / "references" / "runtime-actions.md").read_text(encoding="utf-8").split()
    )
    assert "pb worker wake-ack --wake-id" in text


def test_a_codex_wake_tells_a_held_session_to_acknowledge_it_without_receiving(monkeypatch):
    # W563, coordinator 2026-10-05 23:52 UTC: after .12 was installed, the
    # native wake still said only "Run `pb worker receive --wake-id …`", so a
    # session held for a host window kept receiving old mail one at a time.
    import subprocess

    from project_board.client import codex_queue, session_delivery

    sent = []

    def queue(command, **kwargs):
        sent.append(command[command.index("--message") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(session_delivery, "_codex_executable", lambda: Path("/bin/echo"))
    monkeypatch.setattr(session_delivery.subprocess, "run", queue)
    channel = types.SimpleNamespace(runtime_kind="codex", runtime_session_id="thread-1")

    session_delivery.notify_agent_session(channel, event_kind="input.available", wake_id="wake_held_1")

    message = sent[0]
    assert "`pb worker receive --wake-id wake_held_1`" in message
    assert "held for a host window, run `pb worker wake-ack --wake-id wake_held_1`" in message
    assert "`pb worker inbox`" in message
    # The queue still finds the receive command's wake id, not the wake-ack one.
    assert codex_queue._WAKE_COMMAND.search(message).group("wake_id") == "wake_held_1"
    assert codex_queue._WAKE_COMMAND.search(message).start() < message.index("wake-ack")


def test_mail_a_wake_ack_named_wakes_no_one_again_until_an_ordinary_receive(field):
    # W563, window w563-client-46543807-0335 (codex-app 03:14, Root 03:20 UTC):
    # a held session that answered each wake with wake-ack was woken again
    # every 20 to 30 seconds for the same unread mail, because wake-ack leaves
    # it pending. The acknowledged wake's mail is deferred; new mail still
    # wakes the session, and the next ordinary receive ends the deferral.
    old = _send(field, 1, kind="update", subject="Preserved ordinary mail", body="b")
    field.prepare_worker_session_wake(WORKER, message_refs=[old["message_ref"]], wake_id="wake_held_1")
    before = field.quiet_token(WORKER)

    field.acknowledge_worker_wake(WORKER, wake_id="wake_held_1")

    assert field.quiet_mail_refs(WORKER, refs=[old["message_ref"]]) == {old["message_ref"]}
    assert field.quiet_mail_refs(WORKER) == {old["message_ref"]}
    assert field.quiet_token(WORKER) != before, "the relay and watch caches classify again"
    assert _inbox(field, limit=10)["pending"] == 1, "deferred, not received or settled"

    window = _send(field, 2, kind="request", subject="START: window w-3", body="b")
    assert field.quiet_mail_refs(WORKER, refs=[window["message_ref"]]) == set(), "new mail still wakes"

    pull_worker_input(field, worker_name=WORKER, limit=5)
    assert field.wake_deferred_refs(WORKER) == frozenset()
    assert field.quiet_mail_refs(WORKER, refs=[old["message_ref"]]) == set()


def test_a_wake_ack_never_defers_operator_mail(field):
    from project_board.client.io import content_hash

    payload = {"body": "Approved.", "correlation_id": "approval"}
    operator = field.materialize_control({
        "ref": "work:control:20261006T041500Z:command_deferral:w563-deferral",
        "project_ref": f"work:project:{PROJECT}", "recipient": WORKER, "kind": "reply",
        "subject": "Operator approval", "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })
    field.prepare_worker_session_wake(WORKER, message_refs=[operator["message_ref"]], wake_id="wake_held_2")

    field.acknowledge_worker_wake(WORKER, wake_id="wake_held_2")

    assert operator["message_ref"] in field.wake_deferred_refs(WORKER)
    assert field.quiet_mail_refs(WORKER, refs=[operator["message_ref"]]) == set()

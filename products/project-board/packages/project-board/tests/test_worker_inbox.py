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

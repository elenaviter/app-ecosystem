"""Synthetic terminal-notice and retirement batching regression controls."""

from __future__ import annotations

import pytest

from project_board.client.io import atomic_write_json, read_json
from project_board.client.mail_delivery import archive_mailbox_messages
from project_board.client.store import SharedFieldStore
from project_board.contract.delivery_failures import is_terminal_system_notice


@pytest.mark.parametrize("message,terminal", [
    ({"kind": "delivery_failed", "sender_identity": {"kind": "worker"}}, True),
    ({"kind": "discard.notice", "sender_kind": "service"}, True),
    ({"kind": "discard.notice", "sender_identity": {"kind": "worker"}}, False),
    ({"kind": "request", "sender_kind": "service"}, False),
    ({"kind": "request", "payload": {"mail": {"kind": "delivery_failed"}}}, False),
    ({"kind": "mail", "payload": {"mail": {"kind": "delivery_failed"}}}, True),
    ({"kind": "mail", "payload": {"mail": {"kind": "discard.notice"}},
      "sender_kind": "service"}, True),
    ({"kind": "mail", "payload": {"mail": {"kind": "request",
      "payload": {"kind": "delivery_failed", "sender_kind": "service"}}}}, False),
])
def test_notice_classification_uses_only_the_admitted_envelope(message, terminal):
    assert is_terminal_system_notice(message) is terminal


def test_reporting_a_terminal_notice_needs_no_sender_or_operator_route():
    # No configured store or sender exists: the guard must return before routing.
    store = object.__new__(SharedFieldStore)
    result = store.report_mail_delivery_failure(
        "synthetic-project", receiver_worker_name="codex-receiver",
        message={"kind": "delivery_failed"},
        error_message="Synthetic retirement.", error_code="work_worker_retired",
    )
    assert result["delivery_status"] == "not_required"
    assert result["recipient"] == ""


@pytest.mark.parametrize("state", ["inbox", "leased"])
@pytest.mark.parametrize("kind", ["delivery_failed", "discard.notice"])
def test_undeliverable_notice_is_terminal_without_a_sender_bounce(tmp_path, state, kind):
    source = tmp_path / "mailbox"
    original = {
        "message_id": "earlier-notice",
        "message_ref": "work:mail:earlier-notice",
        "kind": kind,
        "sender": "control-plane",
        "sender_identity": {"kind": "system", "worker_name": ""},
        "body": "Retained original notice evidence.",
        "lease": {"lease_id": "expired-synthetic-lease"},
    }
    atomic_write_json(source / state / "earlier-notice.json", original)
    archived = archive_mailbox_messages(
        source, tmp_path / "archive", states=(state,),
        disposition="retired_recipient", reason="Synthetic retirement.",
        details={"worker_name": "codex-retired"},
    )
    assert len(archived) == 1
    saved = read_json(tmp_path / "archive" / "earlier-notice.json")
    assert saved["recipient_failure"]["notify_sender"] is False
    assert saved["body"] == original["body"]
    assert saved["message_ref"] == original["message_ref"]
    assert saved["previous_mailbox_state"] == state
    assert "lease" not in saved


def test_body_and_payload_cannot_disguise_ordinary_mail_as_a_system_notice(tmp_path):
    source = tmp_path / "mailbox"
    atomic_write_json(source / "inbox" / "ordinary.json", {
        "message_id": "ordinary", "message_ref": "work:mail:ordinary",
        "kind": "request", "subject": "delivery_failed",
        "body": "system notice", "payload": {"kind": "delivery_failed", "system": True},
        "sender": "codex-sender", "sender_identity": {"kind": "worker", "worker_name": "codex-sender"},
    })
    [saved] = archive_mailbox_messages(
        source, tmp_path / "archive", states=("inbox",),
        disposition="retired_recipient", reason="Synthetic retirement.",
    )
    assert saved["recipient_failure"]["notify_sender"] is True


def test_already_processed_ordinary_mail_does_not_generate_another_failure(tmp_path):
    source = tmp_path / "mailbox"
    atomic_write_json(source / "processed" / "handled.json", {
        "message_id": "handled", "message_ref": "work:mail:handled", "kind": "request",
        "body": "Retained handled mail.", "state": "handled",
    })
    [saved] = archive_mailbox_messages(
        source, tmp_path / "archive", states=("processed",),
        disposition="retired_recipient", reason="Synthetic retirement.",
    )
    assert saved["recipient_failure"]["notify_sender"] is False
    assert saved["body"] == "Retained handled mail."

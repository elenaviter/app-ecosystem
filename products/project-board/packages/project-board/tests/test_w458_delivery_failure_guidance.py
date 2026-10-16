"""W458: a delivery_failed notice names a recovery the tools can perform, per refusal code."""
from __future__ import annotations

import pytest

from project_board.client.relay import mail_delivery_failure_guidance


def test_a_missing_project_is_recovered_by_a_fresh_send_naming_the_project():
    text = mail_delivery_failure_guidance("work_project_context_required", {}, outbox_id="outbox_1")
    assert "--project-ref" in text and "new idempotency key" in text
    assert "replay-delivery cannot add one" in text
    assert "No project is chosen for you" in text


@pytest.mark.parametrize("code,details", [
    ("work_worker_not_found", {}),
    ("work_value_required", {"field": "recipient"}),
])
def test_a_refused_recipient_is_replayed_to_the_stable_name(code, details):
    text = mail_delivery_failure_guidance(code, details, outbox_id="outbox_1")
    assert "pb worker replay-delivery --outbox-id outbox_1 --recipient" in text


@pytest.mark.parametrize("code,details", [
    ("work_mail_kind_invalid", {}),
    ("work_value_required", {"field": "kind"}),
])
def test_a_refused_kind_is_replayed_with_a_corrected_kind(code, details):
    text = mail_delivery_failure_guidance(code, details, outbox_id="outbox_1")
    assert "--outbox-id outbox_1" in text and "--kind" in text


@pytest.mark.parametrize("code,details", [
    ("work_value_required", {"field": "body"}),
    ("work_attachment_too_large", None),
    ("anything_else", "not a mapping"),
])
def test_any_other_refusal_never_promises_a_replay_that_cannot_fix_it(code, details):
    text = mail_delivery_failure_guidance(code, details)
    assert "replay-delivery can change only the recipient and the kind" in text
    assert "send the message again" in text
    assert "Correct the rejected field and replay" not in text

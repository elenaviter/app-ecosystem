"""A validation timeout says the action was not sent (W563).

Root, 2026-10-06 04:14-04:18 UTC (W563 note_e2512d98): four sends with
--work-ref waited 90 seconds each on work-reference validation and stopped
with field_plan_authority_deadline_exceeded. The outbox entries they returned
were project.plan.index lookups, and their receipts proved only that the item
resolved, not that a message was delivered.
"""

from __future__ import annotations

import pytest

from project_board.client.plan_authority import await_plan_item_resolution
from project_board.client.render import render_envelope
from project_board.contract.errors import DomainError

WORK_REF = "work:plan:node:20261006T000000Z:w1:synthetic"


class _StalledLookup:
    def outbox_record(self, outbox_id):
        return {"state": "pending", "retry_count": 2}


def test_a_validation_timeout_says_the_action_was_not_sent():
    with pytest.raises(DomainError) as stopped:
        await_plan_item_resolution(
            _StalledLookup(), project_id="one", work_ref=WORK_REF, outbox_id="outbox_lookup1", timeout_seconds=0.1,
        )

    error = stopped.value
    assert error.code == "field_plan_authority_deadline_exceeded"
    assert str(error).startswith("Not sent:")
    assert "Outbox entry outbox_lookup1 is that work-reference lookup, not the action" in str(error)
    assert error.details["stage"] == "work_reference_validation"
    assert error.details["action_sent"] is False
    assert "same idempotency key" in error.details["retry"]

    text = render_envelope({"ok": False, "error": {"code": error.code, "message": str(error), "details": error.details}})
    assert "Not sent:" in text and "action_sent" in text

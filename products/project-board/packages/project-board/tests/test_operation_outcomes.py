"""A receipt that applied is never read as a refusal, whatever its object's state (W262).

2026-09-23, W284: a worker reported ``refused`` on its assignment, the service
applied it (the assignment's state became refused), and the client answered
``ERROR work_operation_refused`` because the receipt's ``state`` field, the
assignment's state, is one of the refused outcome words. The identical retry
was refused the same way instead of reading as a replay.
"""

from __future__ import annotations

import pytest

from project_board.contract.errors import DomainError
from project_board.contract.operation_outcomes import (
    require_applied_operation_outcome,
    require_successful_operation_envelope,
)


def _report_receipt(**overrides):
    receipt = {
        "assignment_ref": "work:assignment:20260923T185635308167Z:assignment_4f9919ed02c24512be2feb60f4d9ffdb:w284",
        "ownership_version": 1,
        "state": "refused",
        "applied": True,
        "replayed": False,
        "disposition": "applied",
        "report": {"state": "refused", "summary": "Folded into W262 by the operator."},
    }
    receipt.update(overrides)
    return receipt


def test_an_applied_report_that_set_the_assignment_to_refused_is_not_a_refusal():
    receipt = _report_receipt()
    assert require_applied_operation_outcome("assignment.report", receipt) is receipt


def test_the_identical_retry_reads_as_a_replay_not_a_refusal():
    receipt = _report_receipt(replayed=True)
    assert require_applied_operation_outcome("assignment.report", receipt) is receipt
    envelope = {"ok": True, "operation": "assignment.report", "object": receipt}
    assert require_successful_operation_envelope("assignment.report", envelope) is envelope


def test_a_receipt_that_did_not_apply_is_still_refused_with_its_reason():
    with pytest.raises(DomainError) as raised:
        require_applied_operation_outcome(
            "assignment.report",
            {"state": "refused", "applied": False, "reason": "work_assignment_version_conflict"},
        )
    assert raised.value.code == "work_assignment_version_conflict"
    with pytest.raises(DomainError) as raised:
        require_applied_operation_outcome("plan.item.update", {"state": "refused"})
    assert raised.value.code == "work_operation_refused"
    assert raised.value.details["outcome_state"] == "refused"

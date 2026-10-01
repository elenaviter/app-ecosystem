from __future__ import annotations

import pytest

from project_board.contract import worker_operation_contract as worker
from project_board.contract.operation_shapes import operation_call_problems, operation_contract

PROJECT = "work:project:synthetic"
BASE = {"work_ref": "work:plan:node:20261001T000000Z:w900:synthetic", "expected_revision": 1, "idempotency_key": "synthetic"}


def test_existing_composite_is_catalogued_without_a_new_card_grant():
    assert "work.item.save" in worker.PROBLEM_BOARD_OPERATIONS
    assert worker.PROBLEM_BOARD_OPERATION_POLICIES["work.item.save"]["composite"] is True
    assert operation_contract("work.item.save")["required"] == ["work_ref", "expected_revision", "idempotency_key"]


@pytest.mark.parametrize("fields,operations", [
    ({"status": "done"}, ("work.status.set",)),
    ({"assignee": "codex-synthetic"}, ("assignment.assign",)),
    ({"assignee": ""}, ("assignment.return",)),
    ({"status": "done", "assignee": "codex-synthetic"}, ("work.status.set", "assignment.assign")),
])
def test_presence_selects_only_existing_step_permissions(fields, operations):
    assert worker.authorization_operations("work.item.save", fields) == operations
    payload = {**BASE, **fields}
    if "assignee" in fields:
        payload["expected_ownership_version"] = 0
    assert operation_call_problems("work.item.save", PROJECT, payload) == []


def test_no_fields_and_missing_assignee_ownership_fence_are_local_errors():
    assert any(problem["field"] == "status | assignee" for problem in operation_call_problems("work.item.save", PROJECT, BASE))
    assert any(problem["field"] == "expected_ownership_version" for problem in operation_call_problems("work.item.save", PROJECT, {**BASE, "assignee": ""}))


@pytest.mark.parametrize("value", ["", None, "not-a-status"])
def test_invalid_supplied_status_is_not_an_omission(value):
    assert any(problem["field"] == "status" for problem in operation_call_problems("work.item.save", PROJECT, {**BASE, "status": value}))

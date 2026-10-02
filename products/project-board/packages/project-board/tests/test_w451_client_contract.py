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
    ({"assignee": "codex-synthetic"}, ("work.assignee.set",)),
    ({"assignee": ""}, ("work.assignee.set",)),
    ({"status": "done", "assignee": "codex-synthetic"}, ("work.status.set", "work.assignee.set")),
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


@pytest.mark.parametrize("assignee", ["codex-synthetic", ""])
def test_generic_assignee_contract_accepts_explicit_clear_and_has_typed_example(assignee):
    import json
    payload = {**BASE, "assignee": assignee, "expected_ownership_version": 0}
    assert operation_call_problems("work.assignee.set", PROJECT, payload) == []
    assert worker.authorization_operations("work.assignee.set", payload) == ("work.assignee.set",)
    contract = operation_contract("work.assignee.set")
    assert {"assignee", "expected_ownership_version"} <= set(contract["required"])
    example = json.loads(contract["example"].split("--payload-json '", 1)[1][:-1])
    assert isinstance(example["expected_revision"], int)
    assert isinstance(example["expected_ownership_version"], int)


@pytest.mark.parametrize("changes,field", [
    ({"assignee": None}, "assignee"), ({"assignee": 123}, "assignee"),
    ({"expected_ownership_version": -1}, "expected_ownership_version"),
    ({"expected_revision": 0}, "expected_revision"),
    ({"work_ref": "invalid"}, "work_ref"),
])
def test_generic_assignee_rejects_malformed_fields(changes, field):
    payload = {**BASE, "assignee": "", "expected_ownership_version": 0, **changes}
    assert any(problem["field"] == field for problem in operation_call_problems("work.assignee.set", PROJECT, payload))


@pytest.mark.parametrize("missing", ["assignee", "expected_ownership_version"])
def test_generic_assignee_requires_field_presence_and_current_ownership(missing):
    payload = {**BASE, "assignee": "", "expected_ownership_version": 0}
    del payload[missing]
    assert any(problem["field"] == missing for problem in operation_call_problems("work.assignee.set", PROJECT, payload))

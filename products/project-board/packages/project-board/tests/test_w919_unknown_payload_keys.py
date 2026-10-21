"""W919: a pb coordinate mutation refuses a payload key it does not take.

Found 2026-10-21 12:00:29Z by claude-ops@spark1: a plan.item.update with
"dry_run": true reached the board, which ignored the unknown key and applied
the change (W909 revision 38). A caller who believes it is previewing must
not write, so an unknown key is refused locally, naming the field, before
anything is sent. Reads keep accepting extra keys.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from project_board.client import cli
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.contract.errors import DomainError
from project_board.contract.operation_shapes import (
    PROBLEM_BOARD_OPERATION_SHAPES,
    operation_call_problems,
    operation_contract,
)
from project_board.contract.worker_operation_contract import operation_is_read

PROJECT = "work:project:quickstart-one"
WORK_REF = "work:plan:node:20261018T161842Z:w909:pb-two-kdcube-profiles:20261021T115900Z-0123"
# The 2026-10-21 W909 call, values abbreviated as in the item.
W909_HOLD = {
    "work_ref": WORK_REF,
    "expected_revision": 37,
    "changes": {"review": {"hold": {"waiting_on": "operator", "due_at": "2026-10-22T12:00:00Z", "reason": "x"}}},
    "idempotency_key": "probe-shape-only",
}


def _args(action, *, payload, config, object_ref=PROJECT):
    return argparse.Namespace(
        action=action, object_ref=object_ref, payload_json=json.dumps(payload), payload_file="",
        contract=False, route="relay", timeout_seconds=30.0, runtime_kind="", runtime_session_id="",
        config=config,
    )


@pytest.fixture
def submits(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(CoordinateQueue, "submit", lambda self, **values: calls.append(values))
    return calls


def _refused(action, payload, tmp_path):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args(action, payload=payload, config=str(tmp_path / "no-relay.json")))
    assert refused.value.code == "work_coordinate_shape_invalid"
    assert "Nothing was sent." in str(refused.value)
    return refused.value.details["problems"]


def test_the_w909_dry_run_call_is_refused_and_nothing_is_sent(submits, tmp_path):
    problems = _refused("plan.item.update", {**W909_HOLD, "dry_run": True}, tmp_path)
    assert [(p["field"], p["problem"]) for p in problems] == [("dry_run", "unknown")]
    assert "dry_run is not a field of plan.item.update" in problems[0]["message"]
    assert submits == [], "no request reaches the relay, so no revision can change"


def test_the_same_call_without_the_unknown_key_has_the_right_shape():
    assert operation_call_problems("plan.item.update", PROJECT, W909_HOLD) == []


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"title": "t", "preview": True}, "changes.preview"),
        ({"review": {"look_at": "a", "could_not_verify": "None", "dry_run": True}}, "changes.review.dry_run"),
        ({"review": {"hold": {"waiting_on": "operator", "due_at": "2026-10-22T12:00:00Z", "reason": "x", "until": "x"}}},
         "changes.review.hold.until"),
    ],
)
def test_unknown_keys_inside_changes_are_refused(submits, tmp_path, changes, field):
    problems = _refused("plan.item.update", {**W909_HOLD, "changes": changes}, tmp_path)
    assert [(p["field"], p["problem"]) for p in problems] == [(field, "unknown")]
    assert submits == []


def test_status_in_changes_stays_the_services_to_refuse():
    # The board's item patch takes PLAN_ITEM_CHANGE_FIELDS and status, and
    # answers a status change there itself; the client does not decide it.
    assert operation_call_problems("plan.item.update", PROJECT, {**W909_HOLD, "changes": {"status": "done"}}) == []


def test_a_read_keeps_accepting_an_extra_key():
    assert operation_is_read("project.plan.item")
    assert operation_call_problems("project.plan.item", PROJECT, {"item_key": "W1", "verbose": True}) == []


@pytest.mark.parametrize(
    ("operation", "field"),
    [("assignment.report", "scope"), ("project.report.publish", "dry_run"), ("worker.restore", "reason")],
)
def test_a_field_the_board_reads_beyond_the_catalog_is_not_refused(operation, field):
    problems = operation_call_problems(operation, "", {field: "x"})
    assert not [p for p in problems if p["problem"] == "unknown"], problems


def test_every_mutation_example_in_the_catalog_passes_the_rule():
    for operation in sorted(PROBLEM_BOARD_OPERATION_SHAPES):
        if operation_is_read(operation):
            continue
        match = re.search(r"--payload-json '(\{.*\})'", operation_contract(operation)["example"])
        if not match:
            continue
        payload = json.loads(match.group(1))
        unknown = [p for p in operation_call_problems(operation, "", payload) if p["problem"] == "unknown"]
        assert unknown == [], (operation, unknown)


def test_every_mutation_accepts_the_idempotency_key():
    # Review of e2439847 (claude-ops@spark1): the key is the transport's, read
    # for outcome recovery and recorded by the board's ledger, on every
    # mutation; 61 of 81 catalog entries do not list it.
    for operation in sorted(PROBLEM_BOARD_OPERATION_SHAPES):
        if operation_is_read(operation):
            continue
        unknown = [p for p in operation_call_problems(operation, "", {"idempotency_key": "k"}) if p["problem"] == "unknown"]
        assert unknown == [], (operation, unknown)


def test_review_assign_with_a_reason_names_what_the_board_would_drop():
    # The reviewer's 2026-10-20 call: the board's review.assign reads work_ref,
    # reviewer and integration, so reason and expected_revision were dropped silently.
    payload = {"work_ref": WORK_REF, "reviewer": "w", "expected_revision": 3, "reason": "r", "idempotency_key": "k"}
    fields = sorted(p["field"] for p in operation_call_problems("review.assign", PROJECT, payload) if p["problem"] == "unknown")
    from project_board.contract.operation_shapes import operation_shape
    described = set(operation_shape("review.assign")["payload"])
    assert "idempotency_key" not in fields
    assert set(fields) == {"expected_revision", "reason"} - described

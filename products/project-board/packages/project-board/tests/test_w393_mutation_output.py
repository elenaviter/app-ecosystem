"""Brief output of a mutation stays bounded as the item and assignment grow (W393).

2026-09-30: an installed ``plan.item.update`` printed 249 lines and 38 KB. The
service answers that operation with the whole updated item, and the renderer
flattened every field: description, search text, summary, acceptance and
every review return. ``work.status.set`` answered with a genuine applied
receipt that nests the item and the whole assignment (task, observed files),
and those were flattened too. The fixtures below have the same shapes, with
synthetic content and bodies grown well past today's sizes, and are rendered
through the real ``pb render`` command as well as the renderer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.render import render_envelope

HIDDEN = "HIDDEN_TAIL"
BODY = "evidence and reasoning " * 1500 + HIDDEN
IDENTITY = "work:plan:node:20260929T112359Z:w93:" + "compact-mutation-output-" * 4
VERSIONED = IDENTITY + ":20260930T013613Z-" + "d" * 64
ASSIGNMENT_REF = "work:assignment:20260929T112622394977Z:assignment_" + "a" * 32 + ":compact-output"
CONTROL_REF = "work:control:20260930T014111Z:command_" + "c" * 32 + ":compact-output"
EVENT_REF = "work:event:20260930T013800Z:event_" + "e" * 32 + ":status-set"
KEY = "w93-status-review-20260930"


def _review_return(index: int) -> dict:
    return {
        "decision": "return",
        "operation": "review.return",
        "reason": f"return {index}: " + BODY,
        "review_ref": f"work:review:{index:04d}:" + "r" * 40,
        "evidence": [BODY],
        "actor": {"principal_key": "card:reviewer"},
        "timestamp": f"2026-09-29T{index % 24:02d}:00:00Z",
    }


def _item(*, returns: int) -> dict:
    """A materialized work item as plan.item.update returns it."""

    return {
        "item_key": "W93",
        "item_id": "item_" + "i" * 32,
        "identity_ref": IDENTITY,
        "item_ref": VERSIONED,
        "title": "Mutation output stays compact",
        "status": "review",
        "revision": 40,
        "assignee": "claude-code-00000000-0000-4000-8000-000000000001",
        "reviewer": "codex-00000000-0000-4000-8000-000000000002",
        "acting_assignee": "claude-code-00000000-0000-4000-8000-000000000001",
        "updated_at": "2026-09-30T01:36:13.239333Z",
        "description": BODY,
        "summary": BODY,
        "search_text": BODY * 3,
        "result": BODY,
        "result_ref": "https://github.com/example/app/pull/361",
        "acceptance": [f"line {index}: {BODY}" for index in range(40)],
        "review": {"look_at": BODY, "could_not_verify": BODY, "submission_contract": BODY},
        "review_history": [_review_return(index) for index in range(returns)],
        "tags": ["cli", "efficiency"],
        "keywords": ["brief output"],
        "body_hash": "b" * 64,
        "search_content_hash": "s" * 64,
    }


def _status_receipt(*, files: int, replayed: bool = False) -> dict:
    """A work.status.set receipt: outcome, compact item, whole assignment."""

    return {
        "state": "applied",
        "applied": True,
        "changed": True,
        "replayed": replayed,
        "operation": "work.status.set",
        "item_ref": IDENTITY,
        "work_ref": VERSIONED,
        "expected_revision": 38,
        "observed_revision": 39,
        "from_status": "review",
        "to_status": "working",
        "result_ref": EVENT_REF,
        "work_item": {
            "item_id": "item_" + "i" * 32,
            "item_ref": VERSIONED,
            "item_key": "W93",
            "title": "Mutation output stays compact",
            "status": "working",
            "item_revision": 39,
            "assignee": "claude-code-00000000-0000-4000-8000-000000000001",
            "reviewer": "",
            "depends_on": [f"work:plan:node:dependency-{index}" for index in range(30)],
            "reviewer_evidence": {"notes": BODY},
        },
        "assignment": {
            "assignment_ref": ASSIGNMENT_REF,
            "current_control_ref": CONTROL_REF,
            "state": "assigned",
            "ownership_version": 11,
            "worker_name": "claude-code-00000000-0000-4000-8000-000000000001",
            "updated_at": "2026-09-30T01:38:00Z",
            "task": {"instructions": BODY, "acceptance": [BODY] * 12},
            "observed_files": {
                "files": [{"path": f"src/module_{index}.py", "sha": "f" * 40} for index in range(files)]
            },
            "reports": [{"summary": BODY}] * 20,
        },
        "summary": "Work status changed to working.",
        "error": {},
        "_mutation_noop": False,
    }


def _brief(obj: dict, *, operation: str, recovery: dict | None = None) -> str:
    result: dict = {"operation": operation, "object": obj}
    if recovery is not None:
        result["recovery"] = recovery
    return render_envelope({"ok": True, "result": result})


def _budget(text: str, *, lines: int, bytes_: int) -> None:
    assert len(text.splitlines()) <= lines, f"{len(text.splitlines())} lines:\n{text}"
    assert len(text.encode("utf-8")) <= bytes_, f"{len(text.encode('utf-8'))} bytes:\n{text}"
    assert HIDDEN not in text, text


RECOVERY = {
    "state": "applied",
    "source": "local_receipt",
    "first_sent_at": "2026-09-30T01:35:56.385498Z",
    "idempotency_key": "w93-current-delivery-review-20260930",
    "request_ids": ["coordinate_" + "q" * 32],
    "attempts": {"coordinate_" + "q" * 32: "applied"},
    "publishing": 0,
    "request_hash": "h" * 64,
}


def test_an_item_returned_by_a_mutation_is_shown_by_its_coordinates() -> None:
    text = _brief(_item(returns=6), operation="plan.item.update", recovery=RECOVERY)
    _budget(text, lines=12, bytes_=1_600)
    lines = text.splitlines()
    assert lines[:2] == ["OK", "operation: plan.item.update"]
    # The relay's recorded outcome is the genuine one; the item has none.
    assert "recovery: applied · source local_receipt · first sent 2026-09-30T01:35:56.385498Z" in lines
    assert "recovery.idempotency_key: w93-current-delivery-review-20260930" in lines
    assert not any(line.startswith("state:") for line in lines), "no receipt state is invented from an item"
    assert "item: W93 · review · Mutation output stays compact" in lines
    assert f"item.identity_ref: {IDENTITY}" in lines
    assert f"item.item_ref: {VERSIONED}" in lines
    assert any(line.startswith("item: revision 40 · ") for line in lines)
    for field in ("description", "search_text", "acceptance", "review_history", "summary"):
        assert field not in text, field


def test_an_item_without_a_recovery_record_still_invents_no_outcome() -> None:
    item = _item(returns=1)
    item["_mutation_replayed"] = True
    item["_mutation_noop"] = True
    lines = _brief(item, operation="plan.item.update").splitlines()
    assert not any(line.startswith(("state:", "recovery")) for line in lines)
    assert "replayed: True" in lines and "unchanged: True" in lines


def test_a_status_receipt_keeps_its_outcome_and_compacts_the_nested_item_and_assignment() -> None:
    text = _brief(_status_receipt(files=40), operation="work.status.set")
    _budget(text, lines=26, bytes_=3_600)
    lines = text.splitlines()
    assert lines[2] == "state: applied", "the outcome is the first line after the operation"
    for expected in (
        "applied = True",
        "changed = True",
        "expected_revision = 38",
        "observed_revision = 39",
        "from_status = review",
        "to_status = working",
        f"item_ref = {IDENTITY}",
        f"work_ref = {VERSIONED}",
        f"result_ref = {EVENT_REF}",
        "summary = Work status changed to working.",
        "work_item: W93 · working · Mutation output stays compact",
        f"work_item.item_ref: {VERSIONED}",
        f"assignment.assignment_ref: {ASSIGNMENT_REF}",
        f"assignment.current_control_ref: {CONTROL_REF}",
    ):
        assert expected in lines, expected
    assert any(line.startswith("work_item: revision 39 · ") for line in lines)
    assert "assignment: state assigned · ownership 11 · worker claude-code-00000000-0000-4000-8000-000000000001 · updated 2026-09-30T01:38:00Z" in lines
    assert "not shown in brief: assignment detail" in lines
    for hidden in ("observed_files", "instructions", "reports", "reviewer_evidence", "_mutation_noop", "error"):
        assert hidden not in text, hidden

    replayed = _brief(_status_receipt(files=1, replayed=True), operation="work.status.set").splitlines()
    assert replayed[2] == "state: applied (replayed)"


def test_output_size_does_not_follow_item_or_assignment_growth() -> None:
    small = _brief(_item(returns=1), operation="plan.item.update", recovery=RECOVERY)
    large = _brief(_item(returns=300), operation="plan.item.update", recovery=RECOVERY)
    assert small == large

    few = _brief(_status_receipt(files=3), operation="work.status.set")
    many = _brief(_status_receipt(files=5_000), operation="work.status.set")
    assert few == many


def test_a_refusal_keeps_its_whole_error_however_large() -> None:
    details = {f"field_{index}": f"reason {index}" for index in range(20)}
    text = _brief(
        {
            "state": "refused",
            "operation": "work.status.set",
            "item_ref": IDENTITY,
            "expected_revision": 38,
            "observed_revision": 41,
            "error": {"code": "work_revision_conflict", "message": "The work item changed.", "details": details},
            "work_item": _status_receipt(files=1)["work_item"],
        },
        operation="work.status.set",
    )
    lines = text.splitlines()
    assert lines[2] == "state: refused"
    assert "error.code = work_revision_conflict" in lines
    for index in range(20):
        assert f"error.details.field_{index} = reason {index}" in lines
    assert "observed_revision = 41" in lines


def test_a_nested_item_envelope_is_shown_by_coordinates_beside_its_own_fields() -> None:
    text = _brief(
        {"item": _item(returns=50), "created": True, "project_ref": "work:project:compact-output"},
        operation="plan.item.create",
    )
    _budget(text, lines=10, bytes_=1_400)
    lines = text.splitlines()
    assert "item: W93 · review · Mutation output stays compact" in lines
    assert f"item.identity_ref: {IDENTITY}" in lines
    assert "created = True" in lines
    assert "project_ref = work:project:compact-output" in lines
    assert "description" not in text


@pytest.mark.parametrize(
    ("operation", "obj", "recovery"),
    [
        ("plan.item.update", _item(returns=30), RECOVERY),
        ("work.status.set", _status_receipt(files=200), None),
    ],
)
def test_pb_render_prints_the_same_bounded_view(tmp_path: Path, capsys, operation, obj, recovery) -> None:
    result: dict = {"operation": operation, "object": obj}
    if recovery is not None:
        result["recovery"] = recovery
    envelope = {"ok": True, "result": result}
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(envelope), encoding="utf-8")

    exit_code = cli.main(["render", "--file", str(saved)])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert printed == render_envelope(envelope)
    _budget(printed, lines=26, bytes_=3_600)


def test_a_replayed_receipt_keeps_its_state_first_and_the_recovery_after_it() -> None:
    """The local ledger adds its recovery record to a replayed receipt (PR369 review).

    The receipt comes back through the real ``_recover_prior_mutation`` path,
    which attaches ``recovery`` beside the receipt. The outcome still leads:
    ``state`` is the line after ``operation``, and the recovery follows it.
    """

    receipt = {"operation": "work.status.set", "object": _status_receipt(files=5, replayed=True)}
    prior = {
        "state": "applied",
        "receipt": receipt,
        "idempotency_key": KEY,
        "request_hash": "h" * 64,
        "request_ids": ["coordinate_" + "q" * 32],
        "first_sent_at": "2026-09-30T01:37:58.000000Z",
    }
    result = cli._recover_prior_mutation(None, None, prior, worker_name="claude-code-test", key=KEY)
    assert result is not None and result["recovery"]["source"] == "local_receipt"

    lines = render_envelope({"ok": True, "result": result}).splitlines()
    assert lines[1] == "operation: work.status.set"
    assert lines[2] == "state: applied (replayed)", "the outcome is the first line after the operation"
    assert lines[3] == "recovery: applied · source local_receipt · first sent 2026-09-30T01:37:58.000000Z"
    assert lines[4] == f"recovery.idempotency_key: {KEY}"

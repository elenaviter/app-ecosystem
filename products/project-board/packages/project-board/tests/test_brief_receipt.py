"""Brief output names the outcome of a governed mutation (W276).

2026-09-23: a retry loop appended the same note to one item fourteen times.
Every call applied. The brief output printed ``error = {}`` beside
``observed_revision``, the caller read that as a conflict, and each retry took
a fresh idempotency key, so replay never engaged. A receipt now prints
``state: applied`` or ``state: refused`` first and drops an empty error.
"""

from __future__ import annotations

from project_board.client.render import render_envelope


def test_an_applied_receipt_names_its_outcome_first_and_prints_no_empty_error():
    text = render_envelope(
        {
            "ok": True,
            "result": {
                "operation": "plan.note.append",
                "object": {
                    "command_ref": "",
                    "state": "applied",
                    "operation": "plan.note.append",
                    "project_ref": "work:project:project-one",
                    "work_ref": "work:plan:node:20260923T000000Z:w1:one",
                    "expected_revision": 4,
                    "observed_revision": 5,
                    "result_ref": "work:note:20260923T000000Z:note_1:one",
                    "summary": "Note appended.",
                    "error": {},
                    "replayed": False,
                    "changed": True,
                },
            },
        }
    )
    lines = text.splitlines()
    assert lines[0] == "OK"
    assert lines[1] == "operation: plan.note.append"
    assert lines[2] == "state: applied", "the outcome is the first thing after the operation"
    assert not any(line.startswith("error") for line in lines), text
    assert "observed_revision = 5" in lines


def test_a_replayed_receipt_is_marked_and_a_refusal_carries_its_reason():
    replayed = render_envelope(
        {
            "ok": True,
            "result": {
                "operation": "plan.note.append",
                "object": {"state": "applied", "replayed": True, "error": {}, "observed_revision": 5},
            },
        }
    ).splitlines()
    assert replayed[2] == "state: applied (replayed)"
    assert not any(line.startswith("error") for line in replayed)

    refused = render_envelope(
        {
            "ok": True,
            "result": {
                "operation": "review.accept",
                "object": {
                    "state": "refused",
                    "applied": False,
                    "reason": "work_item_not_in_review",
                    "summary": "Only work in review can receive a review decision.",
                    "error": {},
                },
            },
        }
    ).splitlines()
    assert refused[2] == "state: refused"
    assert "reason = work_item_not_in_review" in refused
    assert not any(line.startswith("error") for line in refused)

    with_error = render_envelope(
        {
            "ok": True,
            "result": {
                "operation": "plan.note.append",
                "object": {"state": "refused", "error": {"code": "x_code", "message": "why"}},
            },
        }
    ).splitlines()
    assert "error.code = x_code" in with_error and "error.message = why" in with_error


def test_the_receipt_form_is_reserved_for_the_outcome_vocabulary():
    # A project binding reports `bound`, which is not a receipt outcome, so it
    # keeps the flat form and nothing in it is dropped.
    lines = render_envelope(
        {
            "ok": True,
            "result": {
                "operation": "project.binding",
                "object": {"state": "bound", "project_ref": "work:project:p1", "error": {}},
            },
        }
    ).splitlines()
    assert lines[1] == "operation: project.binding"
    assert "state = bound" in lines
    assert not any(line.startswith("state:") for line in lines)
    assert "project_ref = work:project:p1" in lines

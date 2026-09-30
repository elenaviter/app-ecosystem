"""A refused Card operation names the operation, its group and the fix (W262).

On 2026-09-23 `worker.estimate` was declared in the descriptor and every Card
issued before that refused it with `work_worker_operation_not_granted` and a
list of what the Card holds. The operator chose re-approval over a grant
fallback, so the refusal itself has to say which operation, which permission
group, and the fix. Since W420 (2026-09-30) the fix is the normal Card refresh
on the same Card (Refresh worker Card), never a replacement Card.
"""

from __future__ import annotations

import argparse

from project_board.client.card_refusal import (
    CARD_REFRESH_FIX,
    actionable_card_refusal,
    with_actionable_refusal,
)
from project_board.client.cli import _channel_profile
from project_board.client.render import render_envelope

HELD = ["assignment.report", "control.pull", "worker.heartbeat", "worker.rename"]


def test_a_card_whose_list_predates_an_operation_it_holds_the_group_for_gets_the_fix():
    row = actionable_card_refusal(
        "work_worker_operation_not_granted",
        {
            "operation": "worker.estimate",
            "resource": "res:problem-board",
            "reason": "connection_hub_operation_not_granted",
            "granted_operations": HELD,
        },
        profile="dev-main-worker",
    )
    assert row["operation"] == "worker.estimate"
    assert row["permission_group"] == ["work:relay"]
    assert "worker.heartbeat" in row["held_via"]
    assert "predates this operation" in row["why"]
    assert row["fix"] == CARD_REFRESH_FIX
    assert "Refresh worker Card" in row["fix"] and "--replace-card" not in row["fix"]


def test_the_service_named_group_wins_and_a_card_without_the_group_still_gets_the_command():
    row = actionable_card_refusal(
        "work_worker_operation_not_granted",
        {
            "operation": "worker.estimate",
            "reason": "connection_hub_operation_not_granted",
            "required_grants": ["work:coordinate"],
            "granted_operations": ["worker.heartbeat"],
        },
        profile="spark1-worker",
    )
    assert row["permission_group"] == ["work:coordinate"]
    assert row["held_via"] == []
    assert "does not include this operation" in row["why"]
    assert row["fix"] == CARD_REFRESH_FIX
    # An older service that sends no reason still gets the actionable row.
    assert actionable_card_refusal(
        "work_worker_operation_not_granted",
        {"operation": "worker.estimate"},
        profile="",
    )["fix"] == CARD_REFRESH_FIX


def test_other_errors_and_other_reasons_are_left_alone():
    assert actionable_card_refusal("work_worker_card_not_active", {"operation": "worker.estimate"}, profile="p") == {}
    assert actionable_card_refusal(
        "work_worker_operation_not_granted",
        {"operation": "worker.estimate", "reason": "connection_hub_grants_not_granted"},
        profile="p",
    ) == {}
    assert actionable_card_refusal("work_worker_operation_not_granted", {}, profile="p") == {}
    untouched = {"code": "work_assignment_version_conflict", "message": "m", "details": {"current": 2}}
    assert with_actionable_refusal(untouched, profile="p") == untouched


def test_the_cli_output_carries_the_fix_line_and_a_missing_channel_hides_nothing():
    payload = with_actionable_refusal(
        {
            "code": "work_worker_operation_not_granted",
            "message": "The live worker Card does not authorize this Problem Board operation.",
            "details": {
                "operation": "worker.estimate",
                "reason": "connection_hub_operation_not_granted",
                "granted_operations": HELD,
            },
        },
        profile="dev-main-worker",
    )
    text = render_envelope({"ok": False, "error": payload})
    assert text.startswith("ERROR work_worker_operation_not_granted\n")
    assert "  operation = worker.estimate" in text
    assert "  permission_group[0] = work:relay" in text
    assert f"  fix = {CARD_REFRESH_FIX}" in text
    # Reading the profile is best effort: a session with no channel gets an
    # empty profile, and the refusal prints with the placeholder instead of
    # not printing.
    unknown = argparse.Namespace(
        runtime_kind="claude-code",
        runtime_session_id="00000000-0000-4000-8000-000000000000",
        config=None,
    )
    assert _channel_profile(unknown) == ""
    assert isinstance(_channel_profile(argparse.Namespace()), str)

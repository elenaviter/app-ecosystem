"""W602: the published operation contract offers project.set_goal and project.cards.manage.

Problem Board already serves both (its operation rules and dispatch name them),
but the platform's project_board package did not publish them, so a Card could
never hold them and every caller of them was refused as an unknown operation.
Each is published with the exact service grant the current Problem Board route
needs, as an ordinary (not composite) operation of the project kind, with its
request shape. The grant is the service permission only: the owner/admin role
AND the operation on both current Cards are checked by Problem Board itself,
so nothing here confers a role or bypasses a Card.
"""

from __future__ import annotations

import pytest

from project_board.contract.operation_shapes import PROBLEM_BOARD_OPERATION_SHAPES, operation_contract
from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES,
    PROBLEM_BOARD_OPERATIONS,
    PROBLEM_BOARD_OPERATIONS_BY_KIND,
    authorization_operations,
    required_grants_for_operation,
)

EXPECTED = {
    "project.set_goal": {"grants": ("work:coordinate",), "payload": {"goal": "string; empty clears the goal"}},
    "project.cards.manage": {"grants": ("work:admin",), "payload": {}},
}


@pytest.mark.parametrize("operation", sorted(EXPECTED))
def test_each_operation_is_published_with_its_exact_grant_and_shape(operation):
    assert operation in PROBLEM_BOARD_OPERATIONS
    policy = PROBLEM_BOARD_OPERATION_POLICIES[operation]
    assert set(policy) == {"description", "grants"}  # no composite, no role or writer flag
    assert policy["grants"] == EXPECTED[operation]["grants"]
    assert required_grants_for_operation(operation) == frozenset(EXPECTED[operation]["grants"])
    assert authorization_operations(operation, {}) == (operation,)  # checked as itself on both Cards
    contract = operation_contract(operation)
    assert contract["object_ref"] == ["work:project:<project_id>"]
    assert contract["payload"] == EXPECTED[operation]["payload"]
    assert PROBLEM_BOARD_OPERATION_SHAPES[operation]["payload"] == EXPECTED[operation]["payload"]


def test_both_are_project_kind_operations_and_belong_to_no_other_kind():
    for operation in EXPECTED:
        kinds = [kind for kind, operations in PROBLEM_BOARD_OPERATIONS_BY_KIND.items() if operation in operations]
        assert kinds == ["work.project"], (operation, kinds)


def test_card_management_needs_the_admin_service_grant_never_only_coordination():
    """A coordinate-only Card never manages Cards; the goal never needs the admin grant."""
    assert "work:coordinate" not in required_grants_for_operation("project.cards.manage")
    assert "work:admin" not in required_grants_for_operation("project.set_goal")


def test_descriptions_state_the_role_and_card_rule_and_no_problem_board_card_write():
    goal = PROBLEM_BOARD_OPERATION_POLICIES["project.set_goal"]["description"]
    manage = PROBLEM_BOARD_OPERATION_POLICIES["project.cards.manage"]["description"]
    for text in (goal, manage):
        assert "owner or admin role AND this operation on both" in text
    assert "writes no Card in Problem Board" in manage


def test_the_retired_card_editor_says_so_and_the_role_does_not_set_card_operations():
    retired = PROBLEM_BOARD_OPERATION_POLICIES["project.people.card.update"]["description"]
    assert retired.startswith("Retired:") and "work_control_card_edit_in_connection_hub" in retired
    role = PROBLEM_BOARD_OPERATION_POLICIES["project.people.set_role"]["description"]
    assert "sets the operations their project Card holds" not in role  # a role is not a Card preset
    assert "admin minimum on both" in role and "not rewritten" in role

"""W815: project.delete is in the operation contract, admin-managed, with W805's closed request shape."""

from project_board.contract.operation_shapes import PROBLEM_BOARD_OPERATION_SHAPES as OPERATION_SHAPES
from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATIONS_BY_KIND,
    required_grants_for_operation,
)


def test_project_delete_takes_the_managed_admin_grant_like_card_management():
    # op-assist (W815 review): the most destructive operation never sits on a member's or an agent's Card.
    assert required_grants_for_operation("project.delete") == required_grants_for_operation("project.cards.manage")
    assert required_grants_for_operation("project.delete") == frozenset({"work:admin"})


def test_project_delete_belongs_to_the_project_and_has_the_closed_shape():
    assert "project.delete" in PROBLEM_BOARD_OPERATIONS_BY_KIND["work.project"]
    shape = OPERATION_SHAPES["project.delete"]
    assert shape["object_ref"] == "work:project:<project_id>"
    assert set(shape["payload"]) == {"project_ref", "request_id", "phase", "mode", "backup_retention",
                                     "proposal_digest", "confirmation_title"}

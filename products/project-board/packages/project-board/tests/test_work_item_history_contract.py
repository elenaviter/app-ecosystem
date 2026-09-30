"""One canonical bounded history operation, not a browser-only vocabulary."""
from project_board.contract.operation_shapes import operation_contract
from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES, PROBLEM_BOARD_OPERATIONS_BY_KIND,
)


def test_history_is_a_project_observer_operation_with_one_item_selector():
    assert PROBLEM_BOARD_OPERATION_POLICIES["project.plan.history"]["grants"] == ("work:observe",)
    assert "project.plan.history" in PROBLEM_BOARD_OPERATIONS_BY_KIND["work.project"]
    contract = operation_contract("project.plan.history")
    assert contract["object_ref"] == ["work:project:<project_id>"]
    assert contract["one_of"] == [["item_key", "work_ref"]]
    assert set(contract["payload"]) == {"item_key", "work_ref", "cursor", "limit", "generation_token"}
    assert "1..50" in contract["payload"]["limit"] and "20" in contract["payload"]["limit"]

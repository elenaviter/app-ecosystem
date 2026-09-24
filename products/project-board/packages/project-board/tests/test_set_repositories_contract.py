"""The project's repository list is a declared board operation (W304 finding 39, step 2).

The board sets it on the project card, and the heartbeat carries it to every
attending agent's host as assignment_project.repositories, where the agent
sets up its workspace from it.
"""

from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES,
    PROBLEM_BOARD_OPERATIONS_BY_KIND,
)


def test_set_repositories_is_a_coordinate_operation_on_the_project():
    policy = PROBLEM_BOARD_OPERATION_POLICIES["project.set_repositories"]

    assert policy["grants"] == ("work:coordinate",)
    assert "repositories" in policy["description"]
    assert "project.set_repositories" in PROBLEM_BOARD_OPERATIONS_BY_KIND["work.project"]

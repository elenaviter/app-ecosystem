from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES,
    PROBLEM_BOARD_OPERATIONS_BY_KIND,
    required_grants_for_operation,
)


def test_project_repository_mutation_is_a_canonical_project_operation():
    operation = "project.set_repositories"

    assert operation in PROBLEM_BOARD_OPERATION_POLICIES
    assert operation in PROBLEM_BOARD_OPERATIONS_BY_KIND["work.project"]
    assert required_grants_for_operation(operation) == frozenset({"work:coordinate"})

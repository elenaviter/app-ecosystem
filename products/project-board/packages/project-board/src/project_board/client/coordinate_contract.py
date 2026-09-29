"""``pb coordinate`` checks a call against the operation catalog first (W404).

The catalog (``project_board.contract.operation_shapes``) names each
operation's object and payload. ``pb coordinate <operation> --contract``
prints that entry with a copyable command, and every call is checked against
it before the Card relay is used: a misplaced or missing field is one local
error that shows the corrected shape, and nothing is sent.
"""

from __future__ import annotations

import difflib
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.operation_shapes import (
    PROBLEM_BOARD_OPERATION_SHAPES,
    operation_call_problems,
    operation_contract,
)


def _unknown_operation(operation: str) -> DomainError:
    close = difflib.get_close_matches(
        operation, sorted(PROBLEM_BOARD_OPERATION_SHAPES), n=3, cutoff=0.6
    )
    return DomainError(
        "work_coordinate_operation_unknown",
        (
            f"{operation or '(none)'} is not a canonical Problem Board operation."
            + (" Did you mean " + " or ".join(close) + "?" if close else "")
        ),
        status=400,
        details={"operation": operation, "close_matches": close},
    )


def coordinate_contract(operation: str) -> dict[str, Any]:
    """The catalog entry ``pb coordinate <operation> --contract`` prints."""

    contract = operation_contract(operation)
    if not contract:
        raise _unknown_operation(operation)
    return {"operation": "coordinate.contract", "contract": contract}


def require_coordinate_shape(
    operation: str, object_ref: str, payload: Mapping[str, Any]
) -> None:
    """Refuse a call whose shape the catalog already shows is wrong."""

    if operation not in PROBLEM_BOARD_OPERATION_SHAPES:
        raise _unknown_operation(operation)
    problems = operation_call_problems(operation, object_ref, payload)
    if not problems:
        return
    contract = operation_contract(operation)
    raise DomainError(
        "work_coordinate_shape_invalid",
        (
            " ".join(problem["message"] for problem in problems)
            + f" The call's shape: {contract['example']}. Nothing was sent."
        ),
        status=400,
        details={
            "operation": operation,
            "problems": problems,
            "object_ref": contract["object_ref"],
            "required": contract["required"],
            "one_of": contract["one_of"],
            "example": contract["example"],
            "contract_command": f"pb coordinate {operation} --contract",
        },
    )


__all__ = ["coordinate_contract", "require_coordinate_shape"]

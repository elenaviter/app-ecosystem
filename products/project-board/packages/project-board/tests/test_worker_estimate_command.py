"""pb worker busy-until states until when this worker expects to finish (W262).

The operator asked that a worker's expected finish time and a one-line note be
visible to everyone on the project, updated as work happens, kept in the
worker's own record. The command is the worker's side of that.
"""

from __future__ import annotations

import pytest

from project_board.client import cli
from project_board.contract.errors import DomainError
from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES,
    PROBLEM_BOARD_OPERATIONS_BY_KIND,
)


def _args(*argv: str):
    return cli.build_parser().parse_args(["worker", "busy-until", *argv])


def test_the_operation_is_declared_where_the_worker_pool_operations_are():
    assert "worker.estimate" in PROBLEM_BOARD_OPERATION_POLICIES
    assert "worker.estimate" in PROBLEM_BOARD_OPERATIONS_BY_KIND["work.worker"]
    assert "until when" in PROBLEM_BOARD_OPERATION_POLICIES["worker.estimate"]["description"]


def test_the_command_takes_a_utc_time_with_a_note_or_clear():
    args = _args("2026-09-23T21:30Z", "--note", "W262: estimate command, procedure, tests")
    assert args.worker_command == "busy-until"
    assert args.until == "2026-09-23T21:30Z"
    assert args.note.startswith("W262")
    assert args.clear is False
    cleared = _args("--clear")
    assert cleared.clear is True and cleared.until == "" and cleared.note == ""


def test_a_time_without_a_note_and_a_clear_with_a_time_are_refused_before_any_request():
    for argv, code in (
        (("2026-09-23T21:30Z",), "work_worker_estimate_note_required"),
        (("--clear", "--note", "x"), "work_worker_estimate_arguments"),
        ((), "work_worker_estimate_arguments"),
    ):
        with pytest.raises(DomainError) as raised:
            cli._busy_until_payload(_args(*argv))  # noqa: SLF001 - the refusal under test
        assert raised.value.code == code
    assert cli._busy_until_payload(_args("--clear")) == {"busy_until": ""}  # noqa: SLF001
    assert cli._busy_until_payload(_args("2026-09-23T21:30Z", "--note", "W262: tests")) == {  # noqa: SLF001
        "busy_until": "2026-09-23T21:30Z",
        "note": "W262: tests",
    }

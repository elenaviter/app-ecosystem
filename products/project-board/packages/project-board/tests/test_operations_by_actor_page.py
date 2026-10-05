"""docs/operations-by-actor.md lists every catalog operation with the permission it grants.

Operator, 2026-10-05: "docs must be fixed". The page had no row for eleven
catalog operations (review.assign, work.assignee.set and project.role.manage
among them), so it could not answer which permissions a person's Card needs.
The page itself says its rows come from PROBLEM_BOARD_OPERATION_POLICIES; this
test holds it to that.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_board.contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATION_POLICIES,
    required_grants_for_operation,
)

PAGE = Path(__file__).resolve().parents[3] / "docs" / "operations-by-actor.md"
# Decided by the project admin role or an identity rule, not by a Card: the
# page lists them for completeness and the catalog does not carry them.
NOT_CARD_OPERATIONS = {
    "project.people.invitation.withdraw",
    "project.people.remove",
    "project.people.history",
    "project.people.transfer_ownership",
}
ROW = re.compile(r"^\| `(?P<op>[a-z_]+(?:\.[a-z_]+)+)` \| (?P<permission>[^|]*) \| (?P<worker>[^|]*) \| (?P<coordinator>[^|]*) \| (?P<operator>[^|]*) \|", re.M)
DECISIONS = {"yes", "no", "optional", "admin", "admin (role)", "yes (list only)"}


def _rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for match in ROW.finditer(PAGE.read_text(encoding="utf-8")):
        op = match["op"]
        assert op not in rows, f"{op} has two rows"
        rows[op] = {key: match[key].strip() for key in ("permission", "worker", "coordinator", "operator")}
    return rows


def test_every_catalog_operation_has_a_row():
    missing = sorted(set(PROBLEM_BOARD_OPERATION_POLICIES) - set(_rows()))
    assert not missing, f"operations-by-actor.md has no row for {missing}"


def test_only_the_named_non_card_operations_are_outside_the_catalog():
    extra = set(_rows()) - set(PROBLEM_BOARD_OPERATION_POLICIES)
    assert extra == NOT_CARD_OPERATIONS


def test_each_row_names_the_permissions_its_operation_grants():
    rows = _rows()
    for op in PROBLEM_BOARD_OPERATION_POLICIES:
        named = set(re.findall(r"`([^`]+)`", rows[op]["permission"]))
        grants = set(required_grants_for_operation(op))
        assert named == grants, f"{op}: page says {sorted(named)}, catalog grants {sorted(grants)}"


def test_each_row_decides_for_every_actor():
    for op, row in _rows().items():
        for actor in ("worker", "coordinator", "operator"):
            assert row[actor] in DECISIONS, f"{op}: {actor} column is {row[actor]!r}"

"""W418: source approval and final acceptance are separate, in contract and procedure.

W414 (2026-09-30) went Done on an agent's source verdict while its deploy and
the operator's proof were outstanding. ``review.accept`` stays final
acceptance; operator-final work carries ``review_requirement.kind`` operator,
and source approval is existing evidence (a change-request verdict and an item
note). The contract, the review page, the coordinator procedure and the
reviewer rule state the same path.
"""

from __future__ import annotations

from pathlib import Path

from project_board.contract.operation_shapes import PLAN_ITEM_CHANGE_FIELDS

PACKAGE = Path(__file__).resolve().parents[1]
PRODUCT = PACKAGE.parents[1]
PROCEDURES = PACKAGE / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_contract_carries_the_operator_requirement():
    assert PLAN_ITEM_CHANGE_FIELDS["review_requirement"] == {"kind": "qualified | operator"}


def test_the_review_page_owns_source_approval_versus_final_acceptance():
    page = PRODUCT / "docs" / "review.md"
    assert "## Source approval and final acceptance" in page.read_text(encoding="utf-8")
    review = _words(page)
    assert "`review.accept` is final acceptance" in review
    assert "Source approval, a verdict that the submitted source and its evidence are right, is not a board decision." in review
    assert "set when the item is created or routed (`plan.item.update`), and kept by every later edit" in review
    assert "an agent's accept, return or cancel of an operator-final item is refused with `work_review_operator_required`" in review
    assert "the coordinator routes the Review to the final acceptor with `review.assign`" in review
    assert "A worker's `completed` report is never final acceptance." in review


def test_the_coordinator_and_the_reviewer_apply_it():
    coordinator = _words(PROCEDURES / "coordinator.md")
    assert 'set `review_requirement` to `{"kind": "operator"}` when you create or route it' in coordinator
    assert "Never accept such an item as done on source alone" in coordinator
    collaboration = _words(PROCEDURES / "collaboration.md")
    assert "**A source verdict is not final acceptance.**" in collaboration
    assert "ask the coordinator to make such an item operator-final" in collaboration

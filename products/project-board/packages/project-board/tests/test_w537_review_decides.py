"""W537: a review ends in a decision; a hold is named and timed.

On 2026-10-04 W416 and W403 sat in Review behind source approvals while their
remaining work had no author, until the operator asked. The reviewer rule now
returns unmet criteria at once, allows a hold only with a named actor and a
due time recorded on the item, and keeps the finished-source handoff (W455)
for approvals whose remaining gates are merge, activation or the operator.
The coordinator's reconcile looks for reviews that sit.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
PRODUCT = PACKAGE.parents[1]
PROCEDURES = PACKAGE / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_unmet_criteria_go_back_and_a_hold_is_named_and_timed():
    collaboration = _words(PROCEDURES / "collaboration.md")
    assert "**Unmet criteria go back; a review never waits for unowned work.**" in collaboration
    assert "A review ends in one of three decisions, and which one depends on what remains:" in collaboration
    assert "- **Source rework** (a criterion the submitted source does not meet and that needs new work" in collaboration
    assert "decide `review.return` now. Name each unmet criterion, the work it needs, and who you propose does it." in collaboration
    assert "- **Unfinished verification** (your own check of the submitted source is not done" in collaboration
    assert "only this may be held, and only while that step has a **named actor working on it** and a **due time**, both recorded on the item" in collaboration
    assert "When that time passes, or that actor stops attending, decide again at once" in collaboration
    assert '"I keep the review open" without a named actor and a time is a parked item.' in collaboration


def test_the_finished_source_handoff_keeps_precedence():
    collaboration = _words(PROCEDURES / "collaboration.md")
    handoff = collaboration.index("**A finished review hands the item on.**")
    unmet = collaboration.index("**Unmet criteria go back; a review never waits for unowned work.**")
    assert handoff < unmet
    assert "Route it with `review.assign` naming that actor." in collaboration
    assert (
        "- **Post-source gates** (the source is approved and what remains is a merge, an activation or the "
        "operator's check): hand the item on under the bullet above. It is neither held nor returned."
    ) in collaboration
    # A post-source gate is not one of the held cases, and rework is not a handoff.
    rule = collaboration[unmet:collaboration.index("Why: on 2026-10-04 W416 and W403", unmet)]
    held = rule[rule.index("- **Unfinished verification**"):]
    assert "merge" not in held and "operator's check" not in held
    rework = rule[rule.index("- **Source rework**"):rule.index("- **Post-source gates**")]
    assert "review.assign" not in rework and "hand the item on" not in rework


def test_the_coordinator_looks_for_reviews_that_sit():
    coordinator = _words(PROCEDURES / "coordinator.md")
    assert "5. **A review that sits?**" in coordinator
    assert "a hold whose named actor or due time is missing, past, or no longer attending" in coordinator
    assert "When the reviewer is unavailable, route the review to an available one." in coordinator


def test_the_review_page_states_the_same_path():
    review = _words(PRODUCT / "docs" / "review.md")
    assert "A review ends in a decision, chosen by what remains." in review
    assert "Source rework, a criterion the submitted source does not meet and that needs new work, is returned with the reason" in review
    assert "Post-source gates, a merge, an activation or the operator's check after the source is approved, are neither held nor returned" in review
    assert "routed on with `review.assign` to whoever clears the next gate" in review
    assert "Only unfinished verification may be held" in review
    assert "while that step has a named actor working on it and a due time, recorded on the item" in review

"""W1036: an approved design or phase whose builds remain goes back to the builder.

On 2026-11-25 a reviewer gave the W1033 design a GO while every acceptance line
still needed builds. Following the finished-source handoff, it used
`review.assign` to the coordinator, in a project whose facts say the coordinator
does not review; the coordinator routed it straight back, and the item bounced
until the reviewer returned it to the builder with `review.return`.
"""

from __future__ import annotations

from pathlib import Path

from procedure_reference import reference_text

PACKAGE = Path(__file__).resolve().parents[1]
PRODUCT = PACKAGE.parents[1]
PROCEDURES = PACKAGE / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"


def _words(path: Path) -> str:
    return " ".join(reference_text(path).split())


def _approved_phase(collaboration: str) -> str:
    start = collaboration.index("- **Approved phase, builds remain**")
    return collaboration[start:collaboration.index("- **Post-source gates**", start)]


def test_an_approved_phase_is_returned_to_the_builder():
    collaboration = _words(PROCEDURES / "collaboration.md")
    case = _approved_phase(collaboration)
    assert "decide `review.return` to the item's builder now." in case
    assert "name in the return the approval, the next phase or slice, and its order." in case
    assert "Never `review.accept`: the acceptance is not met." in case
    assert 'Never `review.assign` to the coordinator to "route the builds"' in case
    # It sits with the other decisions, after rework and before the post-source handoff.
    unmet = collaboration.index("**Unmet criteria go back; a review never waits for unowned work.**")
    assert collaboration.index("- **Source rework**", unmet) < collaboration.index("- **Approved phase, builds remain**", unmet)


def _handoff(collaboration: str) -> str:
    return collaboration[collaboration.index("**A finished review hands the item on.**"):
                         collaboration.index("**Unmet criteria go back; a review never waits for unowned work.**")]


def test_the_coordinator_is_a_handoff_target_only_for_gates_it_clears():
    collaboration = _words(PROCEDURES / "collaboration.md")
    handoff = _handoff(collaboration)
    assert "This is a handoff of an approved item, not a review" in handoff
    assert (
        "`review.assign` to the coordinator is only for an approval whose next gate is a merge, a deploy or an "
        "operator action that the coordinator itself clears, for example as the item's named merger or as the "
        "merger when none is named"
    ) in handoff
    assert "An approval whose remaining acceptance needs builds is not handed on here" in handoff
    assert "who routes it on, and reviews it only where the project's facts let the coordinator review." in collaboration


def test_a_coordinator_that_does_not_review_still_receives_the_merge():
    """pb-knowledge's W1036 review (P2): the coordinator may be the authorized merger in a project where
    it does no technical review; the rule must route the approved item to it, not forbid the handoff."""
    collaboration = _words(PROCEDURES / "collaboration.md")
    handoff = _handoff(collaboration)
    assert (
        "Where the project's facts say the coordinator does not review, never route a review to it: an "
        "unfinished verification, a returned change or a second opinion goes to a qualified worker. An approved "
        "item it merges is still handed to it."
    ) in handoff
    # The blanket wording that forbade the merger handoff is gone.
    assert "and never where the project's facts say the coordinator does not review: there, name the merger" not in collaboration
    # The routing paragraph (Rule 6, assign to the coordinator) keeps routing, deciding and merging apart from review.
    assert "The coordinator then routes, decides or merges; where the project's facts say the coordinator does not review, it never takes the review itself." in collaboration
    module = _words(PROCEDURES / "coordinator" / "accept-return-cancel.md")
    assert "That is a handoff, not a review. You receive it as the item's merger even where the project's facts say you do not review" in module
    review = " ".join((PRODUCT / "docs" / "review.md").read_text(encoding="utf-8").split())
    assert "even in a project where the coordinator does no technical review; there it merges and never reviews." in review


def test_the_coordinator_module_states_the_same_limits():
    module = _words(PROCEDURES / "coordinator" / "accept-return-cancel.md")
    assert "the project's facts let the coordinator review, with `review.assign` naming yourself" in module
    assert "Where the facts say the coordinator does not review, route every review to a qualified worker; never name yourself" in module
    assert "A reviewer may hand you an item with `review.assign` only for an approval whose next gate is a merge, a deploy or an operator action" in module
    assert "the reviewer returns it to the builder with `review.return`" in module


def test_the_review_page_states_the_same_path():
    review = " ".join((PRODUCT / "docs" / "review.md").read_text(encoding="utf-8").split())
    assert (
        "An approved design or earlier phase whose remaining acceptance needs builds is also returned to the "
        "item's builder with `review.return`, naming the approval and the next phase"
    ) in review
    assert "That handoff is not a review: the coordinator receives it when it clears the gate itself, for example as the merger" in review

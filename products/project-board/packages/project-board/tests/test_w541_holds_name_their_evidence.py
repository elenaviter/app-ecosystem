"""W541: a hold is a claim with evidence; deferred, unrelated or nearby work never makes one.

On 2026-10-04 a deployment was held behind deferred permissions hardening it
did not depend on, with no requirement, evidence or clearing actor named. One
owning rule (collaboration Rule 16) now says what every hold names and what
never makes one, and the window, review and cross-layer places point to it.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REFERENCES = PACKAGE / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _rule() -> str:
    collaboration = _words(REFERENCES / "collaboration.md")
    start = collaboration.index("**A hold is a claim with evidence.**")
    return collaboration[start:collaboration.index("What the author does to carry its part:", start)]


def test_every_hold_names_its_requirement_evidence_clearing_actor_and_checkpoint():
    rule = _rule()
    assert "the **requirement or resource** the held step would affect" in rule
    assert "and the **evidence** that it would" in rule
    assert "the **actor or event that clears it**, the **smallest action** that does, and the **checkpoint**" in rule
    assert "A hold that cannot name these is not a hold, and the step goes ahead." in rule


def test_unrelated_deferred_work_and_proximity_never_make_a_hold():
    rule = _rule()
    assert "deferred or future hardening the step does not depend on" in rule
    assert "a window on another host, or one the step's own runtime does not take part in" in rule
    assert "proximity (the same files, the same day, the same people, a related item)" in rule


def test_a_real_contract_or_schema_dependency_is_a_hold():
    rule = _rule()
    assert "the held layer reads a contract or schema field the other has not shipped" in rule


def test_runtime_only_and_host_client_holds_differ():
    rule = _rule()
    assert "A runtime-only release is held only by an operation in flight it would conflict with" in rule
    assert "a host client switch or relay restart also needs the host window's quiescence (Rule 10)" in rule


def test_a_safety_conflict_is_a_stop_and_doubt_is_one_bounded_check():
    rule = _rule()
    assert "A demonstrated authority or safety conflict is a **stop**" in rule
    assert "one bounded check with the owner of the other side" in rule
    assert "The coordinator decides a hold and keeps it current on the item" in rule
    assert "an available non-author reviewer checks what the gate actually guards" in rule


def test_the_window_review_and_cross_layer_places_point_to_the_one_rule():
    collaboration = _words(REFERENCES / "collaboration.md")
    coordinator = _words(REFERENCES / "coordinator.md")
    assert "it holds nothing that does not depend on it (a hold names its evidence, Rule 16)." in collaboration
    assert 'It is a hold like any other (Rule 16, "A hold is a claim with evidence").' in collaboration
    assert 'Rule 16, "A hold is a claim with evidence"). Deferred hardening, another host\'s window and proximity are never such a dependency.' in coordinator
    # One owning rule: the definition is not repeated elsewhere.
    assert collaboration.count("**A hold is a claim with evidence.**") == 1
    assert "**A hold is a claim with evidence.**" not in coordinator

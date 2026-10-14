"""W853: no doc or procedure tells a person to "press Accept" an operator-reviewed item.

Operator, 2026-10-14: "i do not have any accept! stop talking in this terms. i have change status. and you too. simply
put it to done and assign to one who made it last. this is how we change the status of tasks", and on "press Accept on
the board. Only you can accept it.": "you need to find our from where this leaks every time".
"""

from __future__ import annotations

import re
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[3]
PROCEDURES = PRODUCT / "packages" / "project-board" / "src" / "project_board" / "procedures"
# What a person is told to do that their item dialog does not have.
LEAKS = re.compile(
    r"press(?:es)?\W+accept|click\W+accept|only you can accept|their accept is final|"
    r"requires a person to accept|the person (?:obtains the live proof and )?accepts|"
    r"\bplease\W+accept\b|accept\W+it\W+on\W+the\W+board",
    re.IGNORECASE,
)


def _paragraphs():
    for path in sorted([*(PRODUCT / "docs").rglob("*.md"), *PROCEDURES.rglob("*.md")]):
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            yield path, paragraph


def _forbidden_by_name(paragraph: str, match: re.Match[str]) -> bool:
    """The wording is named only to forbid it: quoted, with "never" earlier in the same sentence."""

    quoted = paragraph[match.start() - 1:match.start()] == '"'
    sentence = re.split(r"[.!?]\s", paragraph[:match.start()])[-1]
    return quoted and "never" in sentence.lower()


def test_no_doc_or_procedure_tells_a_person_to_press_accept():
    found = [
        f"{path.relative_to(PRODUCT)}: {match.group(0)!r}"
        for path, paragraph in _paragraphs()
        for match in LEAKS.finditer(paragraph)
        if not _forbidden_by_name(paragraph, match)
    ]
    assert found == []


def test_a_leak_beside_an_unrelated_never_is_still_found():
    # Mint's W853 review: an exemption for any paragraph that says "never" let a leak through.
    paragraph = 'Never close such an item on source alone. Ask the person to press Accept on the board.'
    assert [m.group(0) for m in LEAKS.finditer(paragraph) if not _forbidden_by_name(paragraph, m)] == ["press Accept"]
    named = 'Their dialog has no Accept, so never write "press Accept" to them.'
    assert [m for m in LEAKS.finditer(named) if not _forbidden_by_name(named, m)] == []


def test_closing_an_operator_reviewed_item_is_named_as_status_and_assignee():
    review = (PRODUCT / "docs" / "review.md").read_text(encoding="utf-8")
    procedure = (PROCEDURES / "problem-board-worker" / "references" / "coordinator" / "accept-return-cancel.md").read_text(encoding="utf-8")
    rule_11 = next((PROCEDURES / "problem-board-worker" / "references" / "collaboration").glob("rule-11-*.md")).read_text(encoding="utf-8")
    for text in (review, procedure):
        flat = " ".join(text.split())
        assert "Status to Done" in flat or "Status Done" in flat
        assert "worked it last" in flat or "did it last" in flat
    assert "set **Assignee** to whoever worked it last" in " ".join(rule_11.split())

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
    r"requires a person to accept|the person (?:obtains the live proof and )?accepts",
    re.IGNORECASE,
)


def _paragraphs():
    for path in sorted([*(PRODUCT / "docs").rglob("*.md"), *PROCEDURES.rglob("*.md")]):
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            yield path, paragraph


def test_no_doc_or_procedure_tells_a_person_to_press_accept():
    # A paragraph may name the wording only to forbid it.
    found = [
        f"{path.relative_to(PRODUCT)}: {LEAKS.search(paragraph).group(0)!r}"
        for path, paragraph in _paragraphs()
        if LEAKS.search(paragraph) and "never" not in paragraph.lower()
    ]
    assert found == []


def test_closing_an_operator_reviewed_item_is_named_as_status_and_assignee():
    review = (PRODUCT / "docs" / "review.md").read_text(encoding="utf-8")
    procedure = (PROCEDURES / "problem-board-worker" / "references" / "coordinator" / "accept-return-cancel.md").read_text(encoding="utf-8")
    rule_11 = next((PROCEDURES / "problem-board-worker" / "references" / "collaboration").glob("rule-11-*.md")).read_text(encoding="utf-8")
    for text in (review, procedure):
        flat = " ".join(text.split())
        assert "Status to Done" in flat or "Status Done" in flat
        assert "worked it last" in flat or "did it last" in flat
    assert "set **Assignee** to whoever worked it last" in " ".join(rule_11.split())

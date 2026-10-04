"""Coordinator traffic is reduced at its source (operator, 2026-10-04).

The operator named message volume itself a broken process. The item is the
status; a handoff is one mail to the next actor; the coordinator gets mail
only for a new decision, an unresolvable blocker or a ready result that is
its own next step, and tracks progress by reading, not by status mail.
"""

from __future__ import annotations

from pathlib import Path

PROCEDURE = Path(__file__).resolve().parents[1] / "src" / "project_board" / "procedures" / "problem-board-worker"


def _words(relative: str) -> str:
    return " ".join((PROCEDURE / relative).read_text(encoding="utf-8").split())


def test_the_item_is_the_status_and_mail_goes_to_whoever_acts_next():
    collaboration = _words("references/collaboration.md")
    assert "5. **The item is the status; mail goes to whoever acts next.**" in collaboration
    assert "A handoff is one actionable mail to the named next actor" in collaboration


def test_the_coordinator_gets_mail_only_for_three_things():
    collaboration = _words("references/collaboration.md")
    assert "The coordinator gets mail only for: a genuinely new scope, authority or safety decision; a blocker you cannot resolve with the actor in front of you; or a ready result whose next step is the coordinator's own" in collaboration
    assert "Never: a routine acknowledgement, a \"still waiting\" or progress mail, the same evidence in a mail and a note, or a team-wide fan-out." in collaboration
    assert "A mail that supersedes an earlier one names that one's reference." in collaboration


def test_the_coordinator_tracks_by_reading_and_filing_needs_no_mail():
    collaboration = _words("references/collaboration.md")
    assert "by reading the items, reports and team state (a bounded pull), not by asking for status mail" in collaboration
    assert "## Rule 14. Search before you file an item " in collaboration + " "
    skill = _words("SKILL.md")
    assert "tell the coordinator what you filed" not in skill
    assert "routing finds new items in the plan" in skill

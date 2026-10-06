"""A machine upgrade waits only for its own team's available agents.

A relay restart affects every agent on the machine, but agents of different
projects cannot yet message each other. Until a per-machine upgrade procedure
exists, the initiating project informs and waits for its available team
agents only, and tells the operator on Telegram which other agents on the
machine to inform (operator, 2026-10-04).
"""

from __future__ import annotations

from pathlib import Path
from procedure_reference import reference_text

REFERENCES = (
    Path(__file__).resolve().parents[1]
    / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"
)


def _words(name: str) -> str:
    return " ".join(reference_text(REFERENCES / name).split())


def test_the_window_waits_for_available_team_agents_only():
    runtime = _words("runtime-actions.md")
    assert "**It informs and waits for the available agents of its own team only.**" in runtime
    assert "**Available, not all:** an agent that is out of tokens, offline, out of sync or not on this team is not asked and not waited for" in runtime
    assert "an unavailable session may be excluded only with evidence" not in runtime


def test_other_agents_on_the_machine_are_listed_to_the_operator_on_telegram():
    runtime = _words("runtime-actions.md")
    assert "**It tells the operator about the other agents on this machine.**" in runtime
    assert "the installer sends the operator a `decision` (it reaches their Telegram) before the window" in runtime
    assert "each such agent to inform (its alias, stable name and project). The window does not wait for them." in runtime


def test_an_available_team_agent_still_keeps_the_gate():
    runtime = _words("runtime-actions.md")
    assert "An affected (available) session that is busy, answers HOLD or misses the acknowledgement without that evidence is **non-quiesced**" in runtime
    collaboration = _words("collaboration.md")
    assert "That gate waits only for the initiating team's available agents, and tells the operator on Telegram about the other agents on the machine" in collaboration


def test_a_held_ready_carries_over_to_a_re_announced_window():
    runtime = _words("runtime-actions.md")
    assert "**A READY carries over to a re-announced window** for the same host and candidate when the participant has stayed held since it gave it" in runtime
    assert "A participant that resumed work after a cancellation, or a window for another host or candidate, needs a fresh READY." in runtime
    assert "an earlier READY for another window is not reusable" not in runtime

"""An enrolled agent guides a new person to a working project (W366).

Operator, 2026-09-26 ~23:20Z: a person tells their agent "i want to create the
project and connect you and other agents to it, help me", and the agent guides
them, repository access included. The worker skill routes that intent to one
procedure, which names who does each step and never lets the agent invent a
value the person owns.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
PROCEDURES = PACKAGE / "src" / "project_board" / "procedures"


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_skill_routes_the_intent_to_the_procedure() -> None:
    skill = _words(PROCEDURES / "problem-board-worker" / "SKILL.md")
    assert "I want to create the project and connect you and other agents to it" in skill
    assert "procedures/create-a-project.md" in skill
    assert "you never invent one" in skill


def test_the_steps_come_in_order() -> None:
    text = (PROCEDURES / "create-a-project.md").read_text(encoding="utf-8")
    headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "0. Agree the plan",
        "1. Create the project on the board",
        "2. Tell the project its repositories",
        "3. Give this machine access to exactly those repositories",
        "4. Add the second agent as a worker",
        "5. Prove it with a first small item",
        "A machine where another person set up pb",
    ]


def test_each_step_names_who_does_it_and_nothing_is_invented() -> None:
    text = (PROCEDURES / "create-a-project.md").read_text(encoding="utf-8")
    sections = text.split("\n## ")[1:]
    for section in sections[:6]:
        first_line_after_heading = [line for line in section.splitlines()[1:] if line.strip()][0]
        assert first_line_after_heading.startswith("*"), section.splitlines()[0]
    words = " ".join(text.split())
    assert "The agent never invents a value the person owns" in words
    assert "Nothing changes until they say yes." in words
    for piece in ("New project", "First worker", "Project", "Repositories", "add-a-worker-host.md) step 7",
                  "Allow write access", "pb worker workspace-report", "pb worker authorize <profile> --device",
                  "Team > Agents > Add agent", "role **worker**"):
        assert piece in words, piece


def test_a_shared_machine_is_answered_plainly() -> None:
    words = _words(PROCEDURES / "create-a-project.md")
    assert "belong to the **operating-system user** that runs the agents, not to a person" in words
    assert "gets their own operating-system user there" in words
    assert "means sharing repository access with that person's agents" in words

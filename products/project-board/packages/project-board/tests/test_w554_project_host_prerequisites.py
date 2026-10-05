"""W554: agents raise a project's missing host prerequisites to the person, with the exact commands.

Operator, 2026-10-05: "the requirements must be known to all agents who work on the project so
that they can rasie the missing requirements to a user during setup and thats up to user t odecide
if they want to install missing stuff on the machine."
"""

from __future__ import annotations

from pathlib import Path

REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "src" / "project_board" / "procedures" / "problem-board-worker" / "references" / "project-workspace.md"
)


def test_the_setup_procedure_checks_the_projects_host_prerequisites_and_tells_the_person():
    text = REFERENCE.read_text(encoding="utf-8")
    section = text[text.index("## 4. Set up the project's development environment"):text.index("## Project files")]
    rule = section[section.index("**Host prerequisites: check them here"):]
    assert "When you set up on a machine, and when\nthat page changes, run each check on this machine." in rule
    assert "the exact install line from the page, and whether it needs an administrator" in rule
    assert "never run an install, `sudo` or group\nchange yourself" in rule
    assert "goes to the coordinator, who adds it" in rule

"""The package README is a stranger's front door on PyPI (W361).

The operator opened project-board on PyPI (2026-09-26) and found an
install-from-source command list: "it does not explain what is PB and why
its needed. It does not explain how to start new machine. how to onboard
agents." These tests keep the README a page a stranger can follow: what
Problem Board is and why, then the whole path from a new machine to an agent
on a project, each step naming who does it, with public links for depth.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REPOSITORY = PACKAGE.parents[3]
README = (PACKAGE / "README.md").read_text(encoding="utf-8")
PUBLIC = "https://github.com/elenaviter/app-ecosystem/blob/main/"

SECTIONS = (
    "What Problem Board is",
    "Why use it",
    "What you need",
    "Set up a new machine",
    "Onboard an agent",
    "Connect the agent to a project",
    "Learn more",
)


def _headings() -> list[str]:
    return re.findall(r"^## (.+)$", README, flags=re.M)


def test_pypi_publishes_this_readme() -> None:
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["readme"] == "README.md"


def test_the_sections_come_in_the_strangers_order() -> None:
    headings = _headings()
    assert [heading for heading in headings if heading in SECTIONS] == list(SECTIONS)


def test_it_explains_the_product_before_any_command() -> None:
    first_command = README.index("```")
    assert first_command > README.index("## Set up a new machine")
    opening = README[: README.index("## Why use it")].lower()
    for idea in ("plan", "assign", "review", "journal", "card", "connection hub", "coordinator"):
        assert idea in opening, idea


def test_every_step_is_numbered_and_names_who_does_it() -> None:
    steps = re.findall(r"^\*\*(\d+)\. [^*]+\*\* \*([^*]+)\*$", README, flags=re.M)
    assert [int(number) for number, _who in steps] == list(range(1, len(steps) + 1))
    assert len(steps) >= 9
    for number, who in steps:
        assert re.search(r"\b(You|you|agent)\b", who), (number, who)


def test_the_path_names_the_commands_a_stranger_types() -> None:
    for command in (
        "python -m pip install --upgrade project-board",
        "pb status",
        "pb setup",
        "pb source use-release --expect-version",
        "pb source status",
        'export PATH="$HOME/.local/bin:$PATH"',
        "pb procedure install",
        "pb relay-service install",
        "Use the problem-board-worker skill.",
        "pb worker authorize <profile>",
        "--device",
        "Team > Agents > Add agent",
        "New project",
        "Team > People",
    ):
        assert command in README, command
    for runtime in ("Claude Code", "Codex"):
        assert runtime in README[README.index("## Onboard an agent"):], runtime


def test_updating_and_rolling_back_is_explained_after_the_setup() -> None:
    update = README.index("### Update or roll back `pb`")
    assert README.index("**4. ") < update < README.index("## Onboard an agent")
    assert "https://pypi.org/project/project-board/#history" in README


def test_links_are_public_and_resolve_in_this_repository() -> None:
    links = re.findall(r"^\[[^\]]+\]: (\S+)$", README, flags=re.M) + re.findall(r"\]\((\S+?)\)", README)
    links = [link for link in links if not link.startswith("https://pypi.org/project/project-board/")]
    assert links
    for link in links:
        assert link.startswith(PUBLIC), f"PyPI renders only absolute public links: {link}"
        assert (REPOSITORY / link[len(PUBLIC):]).is_file(), link
    for page in ("docs/concepts.md", "docs/cards.md", "docs/add-a-machine.md", "procedures/enroll-an-agent.md"):
        assert any(link.endswith(page) for link in links), page


def test_nothing_private_or_internal() -> None:
    for pattern in (
        r"kdcube/applications",
        r"\bW\d{2,}\b",
        r"\bdev-main\b",
        r"\bspark\d\b",
        r"quickstart",
        r"claude-coord",
        r"ngrok",
    ):
        assert not re.search(pattern, README), pattern

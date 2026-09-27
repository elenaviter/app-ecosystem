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
    "What gets installed",
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
        "pb worker authorize <profile> --device",
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


def test_it_says_what_pip_installs_and_what_the_board_needs() -> None:
    part = README[README.index("## What gets installed"): README.index("## Set up a new machine")]
    for piece in ("project-board", "`connection-hub` with its `client` extra", "app-foundation",
                  "service-foundation", "same version", "Card authority", "password store",
                  "KDCube deployment with Connection Hub"):
        assert piece in part, piece
    assert "No KDCube package is installed" in part


def test_the_architecture_diagram_is_shipped_and_shown_by_an_absolute_url() -> None:
    """Operator, 2026-09-27: one SVG showing how everything ties together (W361)."""

    diagram = PACKAGE.parents[1] / "docs" / "assets" / "architecture.svg"
    svg = diagram.read_text(encoding="utf-8")
    assert svg.startswith("<svg") and "<title" in svg and "<desc" in svg
    assert '<rect width="1100"' in svg, "an explicit background, readable on light and dark pages"
    for label in ("Problem Board", "Connection Hub", "pb relay", "native password store", "GitHub", "Telegram"):
        assert f">{label}<" in svg, f"{label} is text, not a path"
    raw = "https://raw.githubusercontent.com/elenaviter/app-ecosystem/main/products/project-board/docs/assets/architecture.svg"
    assert f'<img src="{raw}"' in README, "PyPI renders only an absolute image URL"
    legend = README[README.index(raw): README.index("## Why use it")]
    assert 3 <= sum(1 for line in legend.splitlines() if line.startswith("- **")) <= 5
    for private in ("dev-main", "spark1", "elenaviter@", "ngrok", "quickstart"):
        assert private not in svg, private
    # Operator review, 2026-09-27: the project, the Card hierarchy, plain words
    # for the workspace, and two accents with a legend: teal for Cards and
    # authority, dark red for credentials and keys.
    for label in ("A project", "project Control Card", "a person's Control Card", "that person's My Card",
                  "each agent's Card", "coordinator", "worker", "owner", "admin", "member", "LEGEND"):
        assert f">{label}<" in svg, label
    assert "a worktree per task" in svg and "a review copy at the exact commit" in svg
    assert 'fill="#06968C"' in svg or 'stroke="#06968C"' in svg
    assert 'stroke="#B45438"' in svg


def test_the_agent_guided_path_comes_first_and_the_manual_steps_are_the_fallback() -> None:
    """A stranger's new machine walk (2026-09-27): the agent can help only after `pb procedure install`."""

    setup = README[README.index("## Set up a new machine"): README.index("## Onboard an agent")]
    agent, manual = setup.index("### With your agent"), setup.index("### By hand")
    assert agent < manual
    guided = " ".join(setup[agent:manual].split())
    # The same words as the board's Connect a machine panel (agreed with its author, 2026-09-27).
    for piece in (
        "python3 -m venv ~/.local/share/project-board-bootstrap",
        "~/.local/share/project-board-bootstrap/bin/pip install --upgrade project-board",
        "~/.local/share/project-board-bootstrap/bin/pb procedure install --target claude-code",
        "For Codex, use `--target codex`.",
        'ALIAS=<name>@<machine>',
        'tmux new-session -d -s "$ALIAS" "mkdir -p ~/.kdcube/pb/workspaces/$ALIAS && cd ~/.kdcube/pb/workspaces/$ALIAS && claude --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion"',
        'tmux attach -t "$ALIAS"',
        "Edit only the ALIAS line, for example ana@mint. Use letters, digits, '-' and '@': tmux does not allow '.' and ':' in a session name.",
        "Use the problem-board-worker skill. Help me set up Problem Board on this machine, then enroll this session as a Problem Board worker with alias <name>@<machine>",
        "**Your agent will ask for this:** the endpoint.",
        "**Connect a machine**",
        "`pb worker authorize … --device`",
        "whatever session enrolls is the worker",
    ):
        assert piece in guided, piece
    assert guided.index("pb procedure install") < guided.index("Help me set up Problem Board"), "install the skill first"


def test_what_you_need_names_the_headless_linux_prerequisites_and_who_can_fix_them() -> None:
    need = " ".join(README[README.index("## What you need"): README.index("## What gets installed")].split())
    for piece in ("tmux", "linger", "keyring", "sudo loginctl enable-linger", "any administrator account on the machine can do it", "pb status"):
        assert piece in need, piece


def test_the_worker_start_line_is_the_unattended_one() -> None:
    onboard = " ".join(README[README.index("## Onboard an agent"): README.index("## Connect the agent to a project")].split())
    assert "Whatever session enrolls becomes the worker" in onboard
    assert "claude --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion" in onboard
    assert "tmux new-session -d -s <agent-name>" in onboard


def test_setup_asks_only_for_the_endpoint() -> None:
    """W304 finding 18: the endpoint names the tenant and the project."""

    setup = README[README.index("## Set up a new machine"): README.index("## Onboard an agent")]
    assert "--tenant" not in setup and "--platform-project" not in setup
    assert "**Your agent will ask for this:** the endpoint." in " ".join(setup.split())

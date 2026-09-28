"""The README is a stranger's front door, and help text names no internal items (W361 review return).

The reviewer's five fixes, pinned: every Part 2 step says who does it and what
you see, the steps are lettered so they do not collide with the numbered
"By hand" steps, Codex has its own start lines, the endpoint and the project's
page are said in plain words, the permissions flag is explained, the PATH hint
comes before the second-terminal authorize, and project files are named with a
link. `pb --help` and every subcommand's help carry no W-numbers.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from project_board.client import cli

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme() -> str:
    return " ".join(README.read_text(encoding="utf-8").split())


def test_part_2_steps_are_lettered_and_say_who_and_what_you_see():
    text = _readme()
    for letter, title in zip("ABCDE", (
        "Pick the project", "Add the agent", "Say to your agent",
        "Add this machine's keys on GitHub", "Tell your agent the keys are added",
    )):
        assert f"**{letter}. {title}**" in text, title
    assert "*You, in the board's **Connect a machine** dialog.* Choose one of the projects you administer. You should see" in text
    assert "*You, in the same dialog.* Pick this machine's agent and press **Add to project**. You should see" in text
    assert "skip to step 9, \"Check it works\"" in text
    assert "1. **Pick the project**" not in text


def test_codex_start_lines_sit_next_to_claudes():
    text = _readme()
    assert text.count("codex -C ~/.kdcube/pb/workspaces/$ALIAS --sandbox danger-full-access --ask-for-approval never --search") == 2


def test_plain_words_for_the_endpoint_the_project_page_and_the_permissions_flag():
    text = _readme()
    assert "The endpoint tells `pb` which board to use; you need no project yet" in text
    assert "names the tenant and project" not in text and "project card" not in text
    assert "the project's page on the board" in text
    assert "lets the agent run commands without stopping to ask you each time" in text
    assert "The risk: it can change anything your user account can" in text


def test_the_path_hint_comes_before_the_second_terminal_authorize_and_project_files_are_linked():
    text = _readme()
    approve = text.index("**Approve its Card.**")
    assert text.index("~/.local/bin` on its `PATH`", approve) < text.index("Then run the line", approve)
    assert "([project files][project-files])" in text
    assert "concepts.md#project-files" in README.read_text(encoding="utf-8")


def test_help_texts_name_no_internal_items():
    found: list[str] = []

    def walk(parser: argparse.ArgumentParser, path: str) -> None:
        texts = [parser.description or "", parser.epilog or ""]
        for action in parser._actions:  # noqa: SLF001
            if isinstance(action.help, str):
                texts.append(action.help)
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                texts += [choice.help or "" for choice in action._choices_actions]  # noqa: SLF001
                for name, sub in action.choices.items():
                    walk(sub, f"{path} {name}")
        found.extend(f"{path}: {text[:60]}" for text in texts if re.search(r"\bW\d{2,4}\b", text))

    walk(cli.build_parser(), "pb")
    assert found == []

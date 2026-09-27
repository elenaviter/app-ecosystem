"""Every suggested authorize command carries --device (W367).

Operator, 2026-09-26: agents always suggest `pb worker authorize <profile>
--device`. The person who approves an agent's Card owns it, and may not be the
one signed in to a browser on the machine: a shared or new machine, a second
person, another account. The device link and code let the right person
approve on their own device. The browser callback stays only as the named
fallback when device login fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_board.client import cli, first_run
from project_board.client.authorization import authorization_command
from project_board.client.card_refusal import replace_card_command

PACKAGE = Path(__file__).resolve().parents[1]
PRODUCT = PACKAGE.parents[1]
AUTHORIZE = re.compile(r"pb worker authorize\s+(?!`)([^`|]*)")


def test_listen_and_inspect_suggest_device_login() -> None:
    assert authorization_command("dev-main-worker") == ["pb", "worker", "authorize", "dev-main-worker", "--device"]
    command = authorization_command("dev-main-worker", config_path="/tmp/relay.json")
    assert command[:5] == ["pb", "worker", "authorize", "dev-main-worker", "--device"]


def test_pb_status_suggests_device_login() -> None:
    step = first_run._next_step(
        first_run.SESSION_NOT_ATTENDING,
        config="/tmp/relay.json",
        relay={"installed": True, "running": True},
        session={"enrolled": True, "profile": "dev-main-worker", "authorization": "", "channel_state": "pending_authorization"},
    )
    assert step["step"] == "authorize_profile"
    assert step["command"] == "pb worker authorize dev-main-worker --device"


def test_the_refusals_suggest_device_login() -> None:
    assert replace_card_command("dev-main-worker") == "pb worker authorize dev-main-worker --device --replace-card"
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert '"required_action": f"pb worker authorize {channel.profile} --device"' in source


def _pages() -> list[Path]:
    procedures = PACKAGE / "src" / "project_board" / "procedures"
    return sorted(procedures.rglob("*.md")) + sorted((PRODUCT / "docs").glob("*.md")) + [PACKAGE / "README.md"]


def test_no_page_suggests_authorize_without_device_outside_the_fallback() -> None:
    offenders = []
    for page in _pages():
        text = page.read_text(encoding="utf-8")
        for paragraph in re.split(r"\n\s*\n", text):
            flat = " ".join(paragraph.split())
            if "--callback-port" in flat or "fallback" in flat.lower():
                continue
            for match in AUTHORIZE.finditer(flat):
                arguments = match.group(1).strip()
                if not arguments.startswith(("<", "{")) and not re.match(r"[a-z0-9][\w@.-]*\b", arguments):
                    continue
                if "--device" not in arguments.split(" --no-open")[0]:
                    offenders.append(f"{page.relative_to(PRODUCT)}: pb worker authorize {arguments[:60]}")
    assert offenders == []


def test_the_skill_tells_the_approver_to_use_their_own_device() -> None:
    skill = (PACKAGE / "src/project_board/procedures/problem-board-worker/SKILL.md").read_text(encoding="utf-8")
    step = skill[skill.index("4. Follow `next`"): skill.index("5. Establish the notification path")]
    assert "pb worker authorize <profile> --device" in step
    assert "never drop `--device`" in step
    assert "own device" in step and "pb worker inspect" in step
    assert "only as the named fallback" in step

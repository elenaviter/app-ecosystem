"""An agent publishes one line about itself on every card (W330).

`pb worker info "<text>"` records the line locally; the relay carries it on
its next heartbeat, which every worker Card already holds, so no Card is
re-approved for it. The board answers with the stored line, and the command
then says the line is on the board. `--clear` sends an empty line, which
removes it; a worker that never set one sends no key, so the board keeps what
it has. Teammates read the line in the team of `pb worker context`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from project_board.client import cli, relay
from project_board.contract.errors import DomainError
from test_attendance_materializes_project import PROJECT_REF, Board, _fresh_host

LINE = "Operator: reviews only until 2026-09-30, do not route builds to me."


class InfoBoard(Board):
    """Stores worker_info like the service: absent leaves it, empty clears it."""

    info_text = ""

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat":
            info = (payload or {}).get("worker_info")
            if isinstance(info, dict):
                self.info_text = str(info.get("text") or "")
            answer["object"]["info_text"] = self.info_text
            answer["object"]["team"] = [{"worker_name": self.recipient, "role": "worker", "info_text": self.info_text}]
        return answer


def _heartbeats(board: Board) -> list[dict[str, Any]]:
    return [call["payload"] for call in board.calls if call["action"] == "worker.heartbeat"]


def _cli(identity, *arguments: str) -> dict[str, Any]:
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    return cli._worker_command(cli.build_parser().parse_args(["worker", *arguments[:1], *flags, *arguments[1:]]))  # noqa: SLF001


def test_set_publish_show_clear_and_the_team_reads_it(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    board = InfoBoard(identity.worker_name)
    board.recipient = identity.worker_name
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    # Never set: the heartbeat carries no key, so the board keeps its value.
    asyncio.run(adapter.poll_attendances_once())
    assert all("worker_info" not in payload for payload in _heartbeats(board))
    assert _cli(identity, "info")["rule"].startswith("Never set")

    recorded = _cli(identity, "info", LINE)
    assert recorded["info_text"] == LINE
    assert recorded["on_board"] is False
    assert "within about a minute" in recorded["rule"]

    asyncio.run(adapter.poll_attendances_once())
    assert _heartbeats(board)[-1]["worker_info"] == {"text": LINE}
    shown = _cli(identity, "info")
    assert shown["on_board"] is True and shown["info_text"] == LINE

    # Teammates read it in pb worker context.
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert [member["info_text"] for member in context["team"]] == [LINE]
    # And pb worker list shows the local record on this host.
    listed = cli._worker_command(cli.build_parser().parse_args(["worker", "list"]))  # noqa: SLF001
    assert [row["info"]["text"] for row in listed["workers"] if row.get("info")] == [LINE]

    cleared = _cli(identity, "info", "--clear")
    assert cleared["info_text"] == "" and cleared["on_board"] is False
    asyncio.run(adapter.poll_attendances_once())
    assert _heartbeats(board)[-1]["worker_info"] == {"text": ""}
    assert board.info_text == ""
    assert _cli(identity, "info")["rule"] == "Cleared on the board."


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("info", "first\nsecond"), "field_worker_info_multiline"),
        (("info", "x" * 201), "field_worker_info_too_long"),
        (("info", "   "), "field_worker_info_arguments"),
        (("info", "text", "--clear"), "field_worker_info_arguments"),
    ],
)
def test_a_bad_line_is_refused_before_anything_is_recorded(tmp_path, monkeypatch, arguments, code):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    with pytest.raises(DomainError) as raised:
        _cli(identity, *arguments)
    assert raised.value.code == code
    assert field.worker_info(identity.worker_name) == {}


def test_two_hundred_characters_is_accepted(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    assert _cli(identity, "info", "y" * 200)["info_text"] == "y" * 200

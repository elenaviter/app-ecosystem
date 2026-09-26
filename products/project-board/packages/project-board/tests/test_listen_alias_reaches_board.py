"""A changed alias reaches the board on the heartbeat (W304 U6).

On 2026-09-25 claude-app re-ran `pb worker listen --alias claude-app@spark1`:
`whoami` showed the new alias while the board card kept `claude-app`, because
the board keeps an existing alias on every publish. The operator ruled
(2026-09-26) that no Card is re-approved for a rename. So, like the W330 info
line: `listen --alias` records a request, the relay carries it on the
heartbeat every worker Card already holds (forcing one heartbeat per request,
never one per cycle), and the board's answer (applied, superseded by a later
rename such as the operator's pencil, or refused) shows in `listen` and
`whoami`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from project_board.client import cli, relay
from test_attendance_materializes_project import Board, _fresh_host


class AliasBoard(Board):
    """Applies an alias request like the service: later change wins, absent leaves it."""

    alias = "old-name"
    alias_set_at = ""

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat":
            request = (payload or {}).get("worker_alias_request")
            if isinstance(request, dict):
                at = str(request.get("requested_at") or "")
                if not self.alias_set_at or self.alias_set_at < at:
                    self.alias, self.alias_set_at = str(request["alias"]), at
                    state = "applied"
                else:
                    state = "applied" if self.alias == request["alias"] else "superseded"
                answer["object"]["worker_alias_result"] = {"state": state, "alias": self.alias, "requested_at": at}
        return answer


def _heartbeats(board: Board) -> list[dict[str, Any]]:
    return [call["payload"] for call in board.calls if call["action"] == "worker.heartbeat"]


def _cli(identity, *arguments: str) -> dict[str, Any]:
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    return cli._worker_command(cli.build_parser().parse_args(["worker", *arguments[:1], *flags, *arguments[1:]]))  # noqa: SLF001


def test_listen_records_the_rename_the_relay_carries_it_and_both_commands_show_the_answer(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    board = AliasBoard(identity.worker_name)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    asyncio.run(adapter.poll_attendances_once())
    assert all("worker_alias_request" not in beat for beat in _heartbeats(board)), "never asked: no key"

    listened = _cli(identity, "listen", "--alias", "claude-app@spark1")
    assert listened["board_alias"]["state"] == "requested"
    assert listened["board_alias"]["requested"] == "claude-app@spark1"

    asyncio.run(adapter.poll_attendances_once())
    assert board.alias == "claude-app@spark1"
    carried = [beat["worker_alias_request"] for beat in _heartbeats(board) if "worker_alias_request" in beat]
    assert carried and carried[0]["alias"] == "claude-app@spark1" and carried[0]["requested_at"]

    shown = _cli(identity, "whoami")["channel"]["board_alias"]
    assert shown["state"] == "applied" and shown["alias"] == "claude-app@spark1"

    # Answered: it rides no more heartbeats. The same alias again asks nothing new.
    before = len(_heartbeats(board))
    asyncio.run(adapter.poll_attendances_once())
    assert all("worker_alias_request" not in beat for beat in _heartbeats(board)[before:])
    again = _cli(identity, "listen", "--alias", "claude-app@spark1")
    assert again["board_alias"]["requested_at"] == listened["board_alias"]["requested_at"]


def test_a_later_pencil_rename_stands_and_listen_says_so(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    board = AliasBoard(identity.worker_name)
    board.alias, board.alias_set_at = "Primary reviewer", "2999-01-01T00:00:00Z"  # the operator's pencil, later
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    _cli(identity, "listen", "--alias", "renamed-agent")
    asyncio.run(adapter.poll_attendances_once())
    assert board.alias == "Primary reviewer"
    shown = _cli(identity, "whoami")["channel"]["board_alias"]
    assert shown["state"] == "superseded" and shown["alias"] == "Primary reviewer"
    assert "pencil" in shown["rule"]


def test_a_board_without_the_field_gets_one_forced_heartbeat_per_request_not_one_per_cycle(tmp_path, monkeypatch):
    # An old board never answers, so the request stays pending; like W330's line
    # it may force one heartbeat, then rides only the paced ones.

    def heartbeats_over_ten_cycles(*, linked: bool, rename: bool) -> tuple[int, int]:
        host, identity, field, config = _fresh_host(tmp_path / f"{linked}-{rename}")
        monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
        board = Board(identity.worker_name, linked=linked)  # never answers worker_alias_result
        adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
        asyncio.run(adapter.poll_attendances_once())
        if rename:
            _cli(identity, "listen", "--alias", "renamed-agent")
        before = len(_heartbeats(board))
        for _ in range(10):
            asyncio.run(adapter.poll_attendances_once())
        beats = _heartbeats(board)[before:]
        return len(beats), sum(1 for beat in beats if "worker_alias_request" in beat)

    for linked in (True, False):
        baseline, _ = heartbeats_over_ten_cycles(linked=linked, rename=False)
        with_rename, carrying = heartbeats_over_ten_cycles(linked=linked, rename=True)
        assert with_rename <= baseline + 1, (linked, baseline, with_rename)
        assert carrying >= 1, "the request still reaches the board"


def test_the_procedure_says_the_pencil_and_listen_are_one_alias_and_the_later_wins():
    from project_board.client.procedures import source_package_path

    root = source_package_path().parent
    identity = " ".join((root / "problem-board-worker" / "references" / "identity-and-authorization.md").read_text().split())
    assert "which every worker Card already holds, so no Card is re-approved" in identity
    assert "the operator's pencil on the agent's card and this request are the same alias, and the later change wins" in identity
    assert "A republish never changes a board alias." in identity
    host = " ".join((root / "add-a-worker-host.md").read_text().split())
    assert "the later of it and the operator's pencil on the agent's card wins" in host

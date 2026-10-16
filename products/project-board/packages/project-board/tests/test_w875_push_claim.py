"""W875 B: a relay claims each pushed control and pulls a scope only once per Data Bus connection.

Operator, 2026-10-15 (W875), verbatim: "this pulling is wrong. we have data bus. why its pull if this must be push."
EMain, 2026-10-15 20:28Z: "GO with (i): the claim is control.pull with control_ref". Design review (claude-ops,
note_e9a6713a): B2 claim before handling, B3 hook before the catch-up pull, (a) an idle turn sends only its
heartbeat. Before W875 every turn of every channel sent two ``control.pull`` (project and direct).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from project_board.client import relay

from test_project_files import FILES, PROJECT_REF, FilesBoard, _host, _seeded_remote

CONTROL_REF = "work:control:20261015T203000000000Z:command_0123456789abcdef0123456789abcdef:question"
OTHER_REF = "work:control:20261015T203001000000Z:command_fedcba9876543210fedcba9876543210:another"


class RecordingBoard(FilesBoard):
    """The W370 files board that records every action, keeps the relay's push hooks, and has a connection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.hooks: dict[str, Any] = {}
        self.connection_generation = 1
        self.during_pull = None

    def set_event_hook(self, key, hook) -> None:
        if hook is None:
            self.hooks.pop(key, None)
        else:
            self.hooks[key] = hook

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.actions.append((action, dict(payload or {})))
        if action == "control.pull" and self.during_pull is not None:
            hook, self.during_pull = self.during_pull, None
            hook()
        return await super().action(object_ref=object_ref, action=action, payload=payload)

    def push(self, project_ref: str | None, control_ref: str, *, kind: str = "control.available") -> None:
        refs = {"control_ref": control_ref, **({} if project_ref is None else {"project_ref": project_ref})}
        event = {"type": "problem_board.worker.event.v1", "data": {"kind": kind, "refs": refs}}
        for hook in list(self.hooks.values()):
            hook(event)

    def pulls(self) -> list[dict[str, Any]]:
        return [payload for name, payload in self.actions if name == "control.pull"]

    def reset(self) -> None:
        self.actions.clear()


def _world(tmp_path, monkeypatch, *, generation: Any = 1):
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _seeded_remote(tmp_path / "remotes", "applications", {"docs/release-notes.md": "# Notes\n"})
    board = RecordingBoard(identity.worker_name, remote, files=FILES)
    if generation is None:
        del board.connection_generation
    else:
        board.connection_generation = generation
    # The supervisor's persistent channel adapter is the attendance adapter (project adapters are its children).
    config = dataclasses.replace(config, project_id="attendance")
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    return adapter, board


def _settled(tmp_path, monkeypatch, **kwargs):
    adapter, board = _world(tmp_path, monkeypatch, **kwargs)
    asyncio.run(adapter.poll_attendances_once())  # discovery: the direct scope and the project are found
    asyncio.run(adapter.poll_attendances_once())
    board.reset()
    return adapter, board


def _scopes(pulls: list[dict[str, Any]]) -> list[str]:
    return sorted(str(payload.get("project_ref")) for payload in pulls)


def test_the_first_turns_on_a_connection_pull_each_scope_once(tmp_path, monkeypatch):
    adapter, board = _world(tmp_path, monkeypatch)
    asyncio.run(adapter.poll_attendances_once())
    asyncio.run(adapter.poll_attendances_once())
    assert _scopes(board.pulls()) == ["", PROJECT_REF]
    assert not any("control_ref" in payload for payload in board.pulls())


def test_an_idle_turn_sends_no_control_pull_and_nothing_but_its_heartbeat(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)
    for _ in range(3):
        asyncio.run(adapter.poll_attendances_once())
    sent = {name for name, _payload in board.actions}
    assert "control.pull" not in sent, board.actions
    assert sent <= {"worker.heartbeat"}, sent


def test_a_reconnect_pulls_each_scope_once_more(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)
    board.connection_generation = 2  # the Data Bus client reconnected: pushes sent meanwhile were lost
    asyncio.run(adapter.poll_attendances_once())
    asyncio.run(adapter.poll_attendances_once())
    assert _scopes(board.pulls()) == ["", PROJECT_REF]


def test_a_client_that_reports_no_connection_generation_pulls_every_turn(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch, generation=None)
    asyncio.run(adapter.poll_attendances_once())
    assert _scopes(board.pulls()) == ["", PROJECT_REF]


def test_a_project_push_claims_exactly_that_control(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)

    async def scenario():
        board.push(PROJECT_REF, CONTROL_REF)
        project_adapter = board.hooks[f"problem-board.controls:{PROJECT_REF.rsplit(':', 1)[1]}"].__self__
        await project_adapter._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert board.pulls() == [{
        "project_ref": PROJECT_REF, "lease_owner": adapter.config.relay_id,
        "lease_seconds": max(60, adapter.config.reconcile_ceiling_seconds * 3), "limit": 20,
        "control_ref": CONTROL_REF,
    }]


def test_a_direct_push_is_claimed_in_the_direct_scope(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)

    async def scenario():
        board.push(None, CONTROL_REF)  # worker_stream.notify drops an empty project_ref
        await adapter._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert [(payload["project_ref"], payload["control_ref"]) for payload in board.pulls()] == [("", CONTROL_REF)]


def test_pushes_during_a_claim_are_each_claimed_once_after_it(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)
    hook_key = f"problem-board.controls:{PROJECT_REF.rsplit(':', 1)[1]}"

    async def scenario():
        board.during_pull = lambda: (board.push(PROJECT_REF, OTHER_REF), board.push(PROJECT_REF, OTHER_REF))
        board.push(PROJECT_REF, CONTROL_REF)
        await board.hooks[hook_key].__self__._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert [payload["control_ref"] for payload in board.pulls()] == [CONTROL_REF, OTHER_REF]


def test_a_push_during_the_catch_up_pull_is_claimed_not_lost(tmp_path, monkeypatch):
    """B3: the hook exists before the catch-up pull, so a push that lands during it is claimed after it."""

    adapter, board = _settled(tmp_path, monkeypatch)
    board.connection_generation = 2

    async def scenario():
        board.during_pull = lambda: board.push(None, CONTROL_REF)
        await adapter.poll_attendances_once()
        await adapter._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    direct = [payload for payload in board.pulls() if payload["project_ref"] == ""]
    assert [payload.get("control_ref", "") for payload in direct] == ["", CONTROL_REF]


def test_a_push_without_a_control_ref_pulls_the_scope_as_w794_did(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)

    async def scenario():
        board.push(None, "")
        await adapter._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert board.pulls() == [{
        "project_ref": "", "lease_owner": adapter.config.relay_id,
        "lease_seconds": max(60, adapter.config.reconcile_ceiling_seconds * 3), "limit": 20,
    }]


def test_a_mail_or_plan_command_push_with_a_control_ref_is_claimed_at_once(tmp_path, monkeypatch):
    """W875 B, live 2026-10-15 23:27Z: the board pushes most new controls as ``mail.available`` or
    ``plan.command.available`` (each with its ``control_ref``). The #825 relay claimed only ``control.available``,
    so every mail waited about 65 s for the board's re-push sweep."""

    adapter, board = _settled(tmp_path, monkeypatch)
    project_key = f"problem-board.controls:{PROJECT_REF.rsplit(':', 1)[1]}"

    async def scenario():
        board.push(PROJECT_REF, CONTROL_REF, kind="mail.available")
        await board.hooks[project_key].__self__._immediate_pull  # noqa: SLF001
        board.push(PROJECT_REF, OTHER_REF, kind="plan.command.available")
        await board.hooks[project_key].__self__._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert [(payload["project_ref"], payload.get("control_ref")) for payload in board.pulls()] == [
        (PROJECT_REF, CONTROL_REF), (PROJECT_REF, OTHER_REF),
    ]


def test_a_direct_mail_push_with_a_control_ref_is_claimed_in_the_direct_scope(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)

    async def scenario():
        board.push(None, CONTROL_REF, kind="mail.available")
        await adapter._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert [(payload["project_ref"], payload.get("control_ref")) for payload in board.pulls()] == [("", CONTROL_REF)]


def test_an_event_without_a_control_ref_claims_nothing(tmp_path, monkeypatch):
    adapter, board = _settled(tmp_path, monkeypatch)
    event = {"type": "problem_board.worker.event.v1",
             "data": {"kind": "project.linked", "refs": {"project_ref": PROJECT_REF}}}
    for hook in list(board.hooks.values()):
        hook(event)
    assert board.pulls() == []

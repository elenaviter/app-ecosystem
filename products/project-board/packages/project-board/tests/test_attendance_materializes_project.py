"""An agent linked to a project gets the project's record on its host, and its first receive works (W304 finding 39).

On 2026-09-24 the operator linked claude-ops (spark1) to a project with no
work for it yet. The board's heartbeat carried the project row, but the relay
wrote the local record only for a worker holding an assignment, so the host
never got one and every `pb worker receive` failed with field_record_not_found.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from project_board.client import cli, relay
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore
from project_board.client.io import content_hash
from relay_helpers import make_host

PROJECT_REF = "work:project:quickstart-works-mttfmgqu"
COORDINATOR = "claude-code-dfd0d696-82d2-4bec-8a9c-d94493ec63a5"


class Board:
    """The control plane as a fresh host meets it right after the operator's link."""

    def __init__(self, recipient: str, *, linked: bool = True) -> None:
        self.recipient = recipient
        self.linked = linked
        self.calls: list[dict[str, Any]] = []
        self.pulled = False

    def _mail(self) -> dict[str, Any]:
        payload = {"mail": {"kind": "request", "subject": "Onboarding check 2", "body": "Please reply to confirm you hear me."}}
        return {
            "ref": "work:control:command_787d5f0bee694d2e967b47b1d8c5bf4e",
            "kind": "mail",
            "project_ref": PROJECT_REF,
            "recipient": self.recipient,
            "sender": COORDINATOR,
            "subject": "Onboarding check 2",
            "payload": payload,
            "payload_hash": content_hash(payload),
            "created_at": "2026-09-24T20:39:49Z",
        }

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        self.calls.append({"object_ref": object_ref, "action": action, "payload": body})
        if action == "worker.publish":
            return {"ok": True, "object": {"ref": "work:worker:remote", "pool_status": "active"}}
        if action == "worker.heartbeat":
            if not body.get("project_ref"):
                return {"ok": True, "object": {
                    "ref": "work:worker:remote",
                    "attendances": [{"project_ref": PROJECT_REF, "role": "worker"}] if self.linked else [],
                    "attendance_revision": 1 if self.linked else 0,
                    "host_retirements": [],
                }}
            return {"ok": True, "object": {
                "ref": "work:worker:remote",
                "attendances": [{"project_ref": PROJECT_REF, "role": "worker"}],
                "attendance_revision": 1,
                "assignment_project": {
                    "project_ref": PROJECT_REF,
                    "title": "Quickstart works",
                    "goal": "Onboard agents.",
                    # The project card's repositories (W304 finding 39, step 2 on the board).
                    "repositories_revision": 3,
                    "repositories": [
                        {"alias": "applications", "url": "git@github.com:kdcube/applications.git", "role": "work"},
                        {"alias": "journals", "url": "git@github.com:kdcube/applications.git", "role": "journal", "path": "playground/domain-solution/docs/journal"},
                        {"alias": "broken", "url": "", "role": "work"},
                    ],
                },
                "assignments": [],
                "team": [
                    {"worker_name": COORDINATOR, "worker_alias": "claude-main", "role": "coordinator", "runtime_kind": "claude-code"},
                    {"worker_name": "codex-019fb07f-9251-7f42-a45e-cd212bd5b6c2", "worker_alias": "codex-main", "role": "worker", "runtime_kind": "codex"},
                    {"worker_name": self.recipient, "worker_alias": "claude-ops", "role": "worker", "runtime_kind": "claude-code"},
                ],
            }}
        if action == "control.pull":
            first = not self.pulled and body.get("project_ref") == PROJECT_REF
            self.pulled = self.pulled or first
            return {"ok": True, "object": {"lease_id": "lease-1", "items": [self._mail()] if first else []}}
        return {"ok": True, "object": {"ref": object_ref, "applied": True}}


def _fresh_host(tmp_path, *, listening: bool = True):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    if listening:
        field.listen_worker(identity.worker_name)
    config = dataclasses.replace(
        relay.RelayConfig.from_host_channel(host, channel, project_id=""),
        allowed_peer_workers=("*",),
    )
    return host, identity, field, config


def test_a_link_while_the_relay_runs_is_written_on_the_ping(tmp_path):
    host, identity, field, config = _fresh_host(tmp_path)
    board = Board(identity.worker_name, linked=False)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    asyncio.run(adapter.poll_attendances_once())
    assert not field._project_path("quickstart-works-mttfmgqu").exists()

    # The operator links the agent: the board pushes project.linked to the relay.
    board.linked = True
    assert adapter.request_attendance_refresh(
        kind="project.linked",
        refs={"project_ref": PROJECT_REF, "role": "worker", "attendance_revision": 1},
    )
    asyncio.run(adapter.poll_attendances_once())

    assert field.read_project("quickstart-works-mttfmgqu")["title"] == "Quickstart works"


def test_an_agent_whose_session_was_down_at_link_finds_the_mail_on_its_next_receive(tmp_path):
    host, identity, field, config = _fresh_host(tmp_path, listening=False)
    board = Board(identity.worker_name)
    asyncio.run(relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board).poll_attendances_once())

    # The session comes back later and receives what waited in its mailbox.
    field.listen_worker(identity.worker_name)
    received = pull_worker_input(field, worker_name=identity.worker_name, limit=20, lease_seconds=300)

    assert [item["message"]["subject"] for item in received["items"]] == ["Onboarding check 2"]


def test_a_relay_that_starts_after_the_link_writes_the_project_its_team_and_delivers_the_first_mail(tmp_path, monkeypatch):
    # The relay was down when the operator linked the agent: the attendance is
    # durable on the board and the mail waited there, so its first poll does all of it.
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    field.listen_worker(identity.worker_name)
    config = dataclasses.replace(
        relay.RelayConfig.from_host_channel(host, channel, project_id=""),
        allowed_peer_workers=("*",),
    )
    board = Board(identity.worker_name)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    asyncio.run(adapter.poll_attendances_once())

    project = field.read_project("quickstart-works-mttfmgqu")
    assert project["title"] == "Quickstart works"
    acknowledged = [call for call in board.calls if call["action"] == "control.acknowledge"]
    assert [call["object_ref"] for call in acknowledged] == ["work:control:command_787d5f0bee694d2e967b47b1d8c5bf4e"]

    received = pull_worker_input(field, worker_name=identity.worker_name, limit=20, lease_seconds=300)

    assert [item["message"]["subject"] for item in received["items"]] == ["Onboarding check 2"]

    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    context = cli._worker_command(  # noqa: SLF001 - the command under test
        cli.build_parser().parse_args(
            ["worker", "context", "--runtime-kind", identity.runtime_kind,
             "--runtime-session-id", identity.runtime_session_id, "--project-ref", PROJECT_REF]
        )
    )
    assert context["project_on_this_host"] is True
    assert context["coordinators"] == [COORDINATOR]
    assert {member["worker_alias"] for member in context["team"]} == {"claude-main", "codex-main", "claude-ops"}
    # The workspace is set up from these; an entry without a URL is not kept.
    assert context["repositories_revision"] == 3
    assert [(row["alias"], row["role"]) for row in context["repositories"]] == [("applications", "work"), ("journals", "journal")]


def test_a_receive_before_the_record_exists_lists_the_project_and_goes_on(tmp_path):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    field.listen_worker(identity.worker_name)
    field.sync_worker_attendances(identity.worker_name, [PROJECT_REF])

    received = pull_worker_input(field, worker_name=identity.worker_name, limit=20, lease_seconds=300)

    [project] = received["projects"]
    assert project["state"] == "not_on_this_host"
    assert "relay writes this project's record" in project["note"]

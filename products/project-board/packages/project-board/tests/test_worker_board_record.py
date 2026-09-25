"""An agent reads its own owner and provider account (W304 finding 47).

The board card shows both; `pb worker whoami` and `context` now do too, from
the record the board sends on every heartbeat and the relay keeps.
"""

from __future__ import annotations

import asyncio
from typing import Any

from project_board.client import cli, relay
from test_attendance_materializes_project import PROJECT_REF, Board, _fresh_host

OWN = {
    "worker_name": "",
    "owner": {"user_id": "google-sub-1", "display_name": "Elena", "source": "project_people"},
    "runtime_account": {"account_id": "acct-1", "email": "ops@example.test", "organization": "org-1"},
}


class SelfBoard(Board):
    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat":
            answer["object"]["self"] = dict(OWN)
        return answer


def test_the_relay_keeps_the_workers_own_record_and_both_commands_show_it(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=SelfBoard(identity.worker_name))
    asyncio.run(adapter.poll_attendances_once())

    record = field.worker_board_record(identity.worker_name)
    assert record["owner"] == OWN["owner"]
    assert record["runtime_account"] == OWN["runtime_account"]
    stamped = record["observed_at"]
    # Unchanged on the next heartbeat: not rewritten, and never activity.
    before = field.read_worker(identity.worker_name)["updated_at"]
    assert field.record_worker_board_record(identity.worker_name, OWN) is False
    assert field.worker_board_record(identity.worker_name)["observed_at"] == stamped
    assert field.read_worker(identity.worker_name)["updated_at"] == before

    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    whoami = cli._worker_command(cli.build_parser().parse_args(["worker", "whoami", *flags]))  # noqa: SLF001
    assert whoami["channel"]["board_record"]["owner"]["display_name"] == "Elena"
    assert whoami["channel"]["board_record"]["runtime_account"]["account_id"] == "acct-1"
    context = cli._worker_command(  # noqa: SLF001
        cli.build_parser().parse_args(["worker", "context", *flags, "--project-ref", PROJECT_REF])
    )
    assert context["self"]["owner"]["display_name"] == "Elena"


def test_before_the_first_heartbeat_the_commands_say_it_has_not_arrived(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    whoami = cli._worker_command(cli.build_parser().parse_args(["worker", "whoami", *flags]))  # noqa: SLF001
    assert whoami["channel"]["board_record"]["state"] == "not_received"

"""pb worker whoami names the session's channel, not only its worker name (W304 C11).

On spark1, 2026-09-24, the helper agent ran `pb worker whoami` for an agent
from a plain shell and got the worker name alone: no alias, no profile, and
no sign whether the session was enrolled or still waiting for authorization.
whoami now adds the channel from the host's own configuration.
"""

from __future__ import annotations

from project_board.client import cli, host_config
from project_board.contract.worker_identity import WorkerSessionIdentity

SESSION = "a7b7935d-a064-43ec-937e-2b94f1660b68"


def _whoami(monkeypatch, config):
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(config))
    args = cli.build_parser().parse_args(
        ["worker", "whoami", "--runtime-kind", "claude-code", "--runtime-session-id", SESSION]
    )
    return cli._worker_command(args)  # noqa: SLF001 - the command under test


def _host(tmp_path):
    return host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="spark1",
        allowed_roots=[str(tmp_path)],
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    ).path


def test_an_enrolled_session_shows_its_alias_profile_and_state(tmp_path, monkeypatch):
    config = _host(tmp_path)
    host_config.enroll_worker_channel(
        config,
        identity=WorkerSessionIdentity.create("claude-code", SESSION),
        profile="problem-board-claude-ops",
        worker_alias="claude-ops",
        authorized=False,
    )

    result = _whoami(monkeypatch, config)

    assert result["worker_name"] == f"claude-code-{SESSION}"
    assert result["channel"] == {
        "enrolled": True,
        "worker_alias": "claude-ops",
        "profile": "problem-board-claude-ops",
        "state": "pending_authorization",
    }


def test_a_session_not_enrolled_on_the_host_says_so(tmp_path, monkeypatch):
    assert _whoami(monkeypatch, _host(tmp_path))["channel"] == {"enrolled": False, "reason": "session_not_enrolled"}


def test_a_host_without_configuration_still_answers(tmp_path, monkeypatch):
    result = _whoami(monkeypatch, tmp_path / "missing.json")

    assert result["worker_name"] == f"claude-code-{SESSION}"
    assert result["channel"] == {"enrolled": False, "reason": "host_not_configured"}

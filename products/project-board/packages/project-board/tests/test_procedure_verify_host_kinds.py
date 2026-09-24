"""pb procedure verify checks the agent kinds this host runs (W304 C9).

On spark1, 2026-09-24, the host ran only Claude Code agents and still held a
Codex skill from an earlier setup. `pb procedure verify` checked both kinds by
default and failed on that Codex copy, which no session on the host reads.
Without named targets it now checks the kinds of the host's relay channels,
and says so.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from project_board.client import cli, host_config, procedures
from project_board.client.procedures import install_agent_procedure
from project_board.contract.errors import DomainError
from project_board.contract.worker_identity import WorkerSessionIdentity


def _host(tmp_path, *kinds_and_states):
    host = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    for index, (kind, state) in enumerate(kinds_and_states):
        identity = WorkerSessionIdentity.create(kind, f"11111111-1111-4111-8111-11111111111{index}")
        host_config.enroll_worker_channel(host.path, identity=identity, profile=f"problem-board-{kind}-{index}", authorized=True)
        if state != "active":
            host_config.set_worker_channel_state(host.path, identity=identity, state=state)
    return host.path


def _verify(config, home, target=()):
    return cli._procedure_command(
        SimpleNamespace(procedure_command="verify", target=list(target), home=str(home), config=str(config))
    )


def _stale_codex_skill(home):
    path = procedures._target_path("codex", home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{procedures.LEGACY_PROCEDURE_MARKER}\n# an old worker skill\n", encoding="utf-8")


def test_a_claude_only_host_verifies_claude_code_and_ignores_an_old_codex_skill(tmp_path):
    config = _host(tmp_path, ("claude-code", "active"))
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    _stale_codex_skill(home)

    result = _verify(config, home)

    assert result["targets"] == ["claude-code"]
    assert result["targets_from"] == "host_channels"
    assert [row["target"] for row in result["verified"]] == ["claude-code"]


def test_a_disabled_channel_does_not_make_its_kind_a_target(tmp_path):
    config = _host(tmp_path, ("claude-code", "active"), ("codex", "disabled"))

    targets, source = cli._procedure_targets(SimpleNamespace(target=[], config=str(config)))

    assert (targets, source) == (["claude-code"], "host_channels")


def test_a_named_target_is_checked_as_named(tmp_path):
    config = _host(tmp_path, ("claude-code", "active"))
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    _stale_codex_skill(home)

    with pytest.raises(DomainError) as refused:
        _verify(config, home, target=["codex"])

    assert refused.value.code == "work_agent_procedure_verification_failed"
    assert refused.value.details["targets_from"] == "named"
    assert refused.value.details["installed"][0]["state"] == "stale"


def test_a_host_without_configuration_checks_both_kinds(tmp_path, monkeypatch):
    monkeypatch.delenv("PROBLEM_BOARD_CONFIG", raising=False)

    targets, source = cli._procedure_targets(SimpleNamespace(target=[], config=str(tmp_path / "missing.json")))

    assert (targets, source) == (["codex", "claude-code"], "default")

"""The resume and authorize lines name the session's KDCube profile (W913).

Operator, 2026-10-18: "if the user is admin we can show him the instruction
with the specification of the profile. otherwise regular single profle
instruction". The client writes the profile into the resume line as
`env PB_PROFILE=<name>`; the board removes it for a viewer who is not a
platform super-admin. The authorize line printed for a second terminal names
the profile too, because that terminal has no PB_PROFILE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client import host_config
from project_board.client.authorization import authorization_command
from project_board.client.host_config import HostRelayConfig
from project_board.client.relay import _host_profile_name
from project_board.client.resume_command import build_session_resume_command
from project_board.contract.errors import DomainError

PROFILE = {"config_dir": "/home/ana/.claude-accounts/ana@example.com", "executable": "/usr/local/bin/claude"}


def _setup(tmp_path: Path, target: str, *, project: str = "project") -> Path:
    root = tmp_path / "roots" / target
    root.mkdir(parents=True, exist_ok=True)
    return host_config.initialize_host_config(
        target_id=target,
        endpoint=f"https://{target}.example/mcp",
        tenant="tenant",
        platform_project=project,
        host_id="host-one",
        allowed_roots=[str(root)],
        source_repositories={},
    ).path


def _claude(kdcube_profile: str = "") -> str:
    return build_session_resume_command(
        runtime_kind="claude-code",
        runtime_session_id="s-1",
        working_directory="/home/ana/work",
        profile=PROFILE,
        kdcube_profile=kdcube_profile,
    )["command"].splitlines()[-1].split(" && ", 1)[-1]


def test_a_claude_line_of_a_named_profile_starts_with_pb_profile():
    assert _claude("second").startswith(
        "env PB_PROFILE=second CLAUDE_CONFIG_DIR=/home/ana/.claude-accounts/ana@example.com "
        "/usr/local/bin/claude --resume s-1"
    )


def test_a_codex_line_of_a_named_profile_runs_through_env():
    line = build_session_resume_command(
        runtime_kind="codex", runtime_session_id="s-2", working_directory="/home/ana/work", kdcube_profile="second"
    )["command"].splitlines()[-1]
    assert line.startswith("env PB_PROFILE=second codex resume s-2 ")


def test_without_a_profile_the_lines_are_as_before():
    assert _claude().startswith("env CLAUDE_CONFIG_DIR=")
    line = build_session_resume_command(
        runtime_kind="codex", runtime_session_id="s-2", working_directory="/home/ana/work"
    )["command"].splitlines()[-1]
    assert line.startswith("codex resume s-2 ")


def test_a_profile_name_that_would_need_quoting_is_refused():
    with pytest.raises(DomainError) as refused:
        _claude("ai pb;rm")
    assert refused.value.code == "work_session_resume_kdcube_profile_invalid"


def test_the_relay_names_the_profile_of_its_own_config(tmp_path):
    local = _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    # The default profile is named too: on a super-admin's screen it is explicit.
    assert _host_profile_name(HostRelayConfig.load(local)) == "local"
    assert _host_profile_name(HostRelayConfig.load(cloud)) == "cloud"


def test_an_ambiguous_profile_name_is_left_out(tmp_path):
    one = _setup(tmp_path, "cloud", project="one")
    _setup(tmp_path, "cloud", project="two")
    assert _host_profile_name(HostRelayConfig.load(one)) == ""


def test_the_second_terminal_reaches_the_same_profile(tmp_path):
    local = _setup(tmp_path, "local")
    cloud = _setup(tmp_path, "cloud")
    assert host_config.selection_arguments(local, target_id="local") == []
    assert host_config.selection_arguments(cloud, target_id="cloud") == ["--profile", "cloud"]
    assert authorization_command("w-1", selection=["--profile", "cloud"]) == [
        "pb", "worker", "authorize", "w-1", "--device", "--profile", "cloud",
    ]

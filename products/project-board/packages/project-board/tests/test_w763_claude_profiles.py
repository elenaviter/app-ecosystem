"""Each Claude session reports and resumes under its own account profile (W763).

Operator, 2026-10-12: "we agreed earlier that we will capture email per
session. and that email will be profile name. each agent session belong to
specific account and we store this information already. so you will be able
to go to proper profile folder to get the usage on that account".

Two sessions on one host can run under two accounts through
`CLAUDE_CONFIG_DIR`. The host's global login says nothing about either, a
profile is named by its account email, the account ID in the profile's own
`.claude.json` is what is verified, and a resume line names the profile and
the absolute executable so that no shell function called `claude` can change
it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import claude_profile as cp
from project_board.client import host_config
from project_board.client.procedures import claude_config_root
from project_board.client.relay import ProblemBoardHostRelayAdapter
from project_board.client.resume_command import build_session_resume_command
from project_board.contract.errors import DomainError
from project_board.contract.worker_identity import WorkerSessionIdentity

SESSION_A = "1815da0e-8302-499b-b371-ba3a3a7fe17c"
SESSION_B = "2a2f3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
ACCOUNT_A = "aaaaaaaa-1111-4222-8333-444444444444"
ACCOUNT_B = "bbbbbbbb-1111-4222-8333-444444444444"
ACCOUNT_GLOBAL = "cccccccc-1111-4222-8333-444444444444"


def _login(folder: Path, account_id: str, email: str) -> Path:
    """A profile folder logged in to one account: public fields only, as Claude writes them."""

    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": account_id, "emailAddress": email, "organizationUuid": "org-1"}}),
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    # The host's global login belongs to a third account: it must never leak
    # into a bound session's report.
    (root / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": ACCOUNT_GLOBAL, "emailAddress": "global@example.com"}}),
        encoding="utf-8",
    )
    return root


def _profile(home: Path, email: str, account_id: str) -> Path:
    return _login(home / ".claude-accounts" / email, account_id, email)


# -- naming ---------------------------------------------------------------


def test_profile_folder_is_named_by_the_account_email(home):
    assert cp.profile_dir_for_email("Ana@Example.com", home=home) == home / ".claude-accounts" / "ana@example.com"


@pytest.mark.parametrize(
    "email",
    ["", "ana", "../ana@example.com", "a/b@example.com", "ana @example.com", "ana@example", ".ana@example.com",
     "ana@exa..mple.com", "ana@example.com\nx"],
)
def test_an_email_that_is_not_one_plain_address_is_refused(home, email):
    with pytest.raises(DomainError) as caught:
        cp.profile_dir_for_email(email, home=home)
    assert caught.value.code == "work_claude_profile_email_invalid"


# -- binding at enrollment --------------------------------------------------


def test_a_session_started_in_its_account_folder_is_bound_and_registered(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder), "PATH": ""}, home=home)
    assert binding["config_dir"] == str(folder)
    assert binding["email"] == "ana@example.com"
    assert binding["account_id"] == ACCOUNT_A
    assert binding["bound_by"] == "session-capture"
    assert cp.read_registry(home)[ACCOUNT_A]["config_dir"] == str(folder)


def test_a_session_on_the_global_login_is_left_for_the_operator_to_map(home):
    binding = cp.capture_binding(environ={}, home=home)
    assert binding["state"] == cp.UNBOUND
    assert binding["reason"] == "started_without_profile"
    assert "config_dir" not in binding and "account_id" not in binding


def test_a_folder_not_named_by_its_account_is_not_bound(home):
    # The colleague's layout: ~/.claude-work holds the account's login.
    folder = _login(home / ".claude-work", ACCOUNT_A, "ana@example.com")
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    assert binding["state"] == cp.UNBOUND
    assert binding["reason"] == "started_folder_is_not_the_account_folder"
    assert binding["expected_config_dir"] == str(home / ".claude-accounts" / "ana@example.com")


def test_a_new_start_never_switches_a_confirmed_binding(home):
    folder_a = _profile(home, "ana@example.com", ACCOUNT_A)
    folder_b = _profile(home, "bob@example.com", ACCOUNT_B)
    first = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_a)}, home=home)
    again = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_b)}, home=home, existing=first)
    assert again["config_dir"] == str(folder_a)
    assert again["account_id"] == ACCOUNT_A
    assert again["capture"] == "differs_from_binding"


def test_a_folder_mapped_to_another_account_is_refused_not_rebound(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    # Another account logs in inside the same folder.
    _login(folder, ACCOUNT_B, "ana@example.com")
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    assert binding["state"] == cp.UNBOUND
    assert binding["reason"] == "work_claude_profile_folder_mapped"
    assert cp.read_registry(home)[ACCOUNT_A]["config_dir"] == str(folder)


def test_a_changed_account_email_keeps_its_registered_folder(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    _login(folder, ACCOUNT_A, "ana.new@example.com")
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    assert binding["config_dir"] == str(folder)
    assert not (home / ".claude-accounts" / "ana.new@example.com").exists()


def test_operator_binding_of_a_legacy_session_uses_its_stored_email(home):
    binding = cp.bind_session(email="Ana@Example.com", account_id=ACCOUNT_A, executable="/opt/bin/claude", home=home)
    assert binding["config_dir"] == str(home / ".claude-accounts" / "ana@example.com")
    assert binding["bound_by"] == "operator"
    # Until the operator logs that folder in, the account is not verified.
    assert cp.profile_state(binding, home=home) == (cp.UNREADABLE, {})


def test_enrolling_again_keeps_the_binding_and_a_state_change_keeps_it_too(tmp_path, home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
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
    identity = WorkerSessionIdentity.create("claude-code", SESSION_A)
    channel = host_config.enroll_worker_channel(
        host.path,
        identity=identity,
        profile="problem-board-claude-one",
        authorized=True,
        claude_profile=lambda existing: cp.capture_binding(
            environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home, existing=existing
        ),
    )
    assert channel.claude_profile["account_id"] == ACCOUNT_A
    # A later start on the global login never drops or switches the binding.
    again = host_config.enroll_worker_channel(
        host.path,
        identity=identity,
        authorized=True,
        claude_profile=lambda existing: cp.capture_binding(environ={}, home=home, existing=existing),
    )
    assert again.claude_profile["config_dir"] == str(folder)
    assert again.claude_profile["capture"] == "differs_from_binding"
    changed = host_config.set_worker_channel_state(host.path, identity=identity, state="disabled")
    assert changed.claude_profile["config_dir"] == str(folder)
    loaded = host_config.HostRelayConfig.load(host.path).worker(identity)
    assert loaded.claude_profile["account_id"] == ACCOUNT_A


def test_the_channel_record_round_trips_its_binding():
    row = {
        "runtime_kind": "claude-code",
        "runtime_session_id": SESSION_A,
        "profile": "p1",
        "claude_profile": {"config_dir": "/x/.claude-accounts/ana@example.com", "account_id": ACCOUNT_A},
        "runtime_launch": {"argv": ["claude"]},
    }
    channel = host_config.WorkerChannelConfig.from_mapping(row)
    assert channel.to_mapping()["claude_profile"] == row["claude_profile"]
    from dataclasses import replace

    assert replace(channel, state="disabled").to_mapping()["claude_profile"] == row["claude_profile"]


# -- account and usage per session -------------------------------------------


def test_two_concurrent_profiles_each_report_their_own_account_never_the_global_login(home):
    a = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(_profile(home, "ana@example.com", ACCOUNT_A))}, home=home)
    b = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(_profile(home, "bob@example.com", ACCOUNT_B))}, home=home)
    state_a, account_a = cp.profile_state(a, home=home)
    state_b, account_b = cp.profile_state(b, home=home)
    assert (state_a, account_a["account_id"]) == (cp.VERIFIED, ACCOUNT_A)
    assert (state_b, account_b["account_id"]) == (cp.VERIFIED, ACCOUNT_B)
    # Logging the global login in and out changes neither.
    (home / ".claude.json").unlink()
    assert cp.profile_state(a, home=home)[1]["account_id"] == ACCOUNT_A


def test_a_profile_logged_in_to_another_account_attributes_nothing(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    _login(folder, ACCOUNT_B, "bob@example.com")
    assert cp.profile_state(binding, home=home) == (cp.MISMATCH, {})


def test_an_unbound_or_unreadable_profile_attributes_nothing(home):
    assert cp.profile_state({}, home=home) == (cp.UNBOUND, {})
    binding = {"config_dir": str(home / ".claude-accounts" / "gone@example.com"), "account_id": ACCOUNT_A}
    assert cp.profile_state(binding, home=home) == (cp.UNREADABLE, {})


SAMPLE = {"observed_at": "2026-10-12T20:00:00Z", "week_used_percentage": 40}


def test_usage_from_a_bound_session_names_its_verified_profile_account(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    state = cp.qualify_profile_limit_state(
        SAMPLE, binding, environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home
    )
    assert state["account_id"] == ACCOUNT_A
    assert state["account_source"] == "session_profile"


@pytest.mark.parametrize("started", ["other", "global"])
def test_usage_from_a_process_outside_the_bound_profile_names_no_account(home, started):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    other = _profile(home, "bob@example.com", ACCOUNT_B)
    environ = {"CLAUDE_CONFIG_DIR": str(other)} if started == "other" else {}
    state = cp.qualify_profile_limit_state(SAMPLE, binding, environ=environ, home=home)
    assert "account_id" not in state


def test_usage_from_a_mismatched_profile_names_no_account(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    _login(folder, ACCOUNT_B, "bob@example.com")
    state = cp.qualify_profile_limit_state(
        SAMPLE, binding, environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home
    )
    assert "account_id" not in state


def test_usage_from_a_process_on_the_default_folder_names_no_account(home):
    state = cp.qualify_profile_limit_state(SAMPLE, {}, environ={}, home=home)
    assert "account_id" not in state


def _adapter(reader):
    return SimpleNamespace(
        _runtime_account_reader=reader,
        _runtime_account_error_state={},
        config=SimpleNamespace(worker_name="w"),
    )


def test_relay_reports_a_bound_session_as_its_own_profile(home):
    binding = cp.capture_binding(
        environ={"CLAUDE_CONFIG_DIR": str(_profile(home, "ana@example.com", ACCOUNT_A))}, home=home
    )
    adapter = _adapter(lambda: cp.read_session_account(binding, home=home))
    account, evidence = asyncio.run(ProblemBoardHostRelayAdapter._runtime_account(adapter))
    assert account["account_id"] == ACCOUNT_A
    assert evidence["state"] == "reported"
    assert evidence["source"] == "session-profile"
    assert evidence["profile_state"] == cp.VERIFIED


def test_relay_reports_a_mismatched_profile_as_missing_with_the_reason(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home)
    _login(folder, ACCOUNT_B, "bob@example.com")
    adapter = _adapter(lambda: cp.read_session_account(binding, home=home))
    account, evidence = asyncio.run(ProblemBoardHostRelayAdapter._runtime_account(adapter))
    assert account == {}
    assert (evidence["state"], evidence["source"], evidence["profile_state"]) == (
        "missing", "session-profile", cp.MISMATCH,
    )


# -- resume lines ---------------------------------------------------------------


def _fake_claude(bin_dir: Path) -> Path:
    """An executable named claude that prints the profile it was given and its arguments."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "claude"
    exe.write_text('#!/bin/sh\nprintf "dir=%s\\n" "$CLAUDE_CONFIG_DIR"\nprintf "arg=%s\\n" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _resume(tmp_path: Path, email: str, *, tmux: str = "") -> tuple[dict, Path, Path]:
    exe = _fake_claude(tmp_path / "local bin")
    profile = tmp_path / "accounts dir" / email
    profile.mkdir(parents=True, exist_ok=True)
    cwd = tmp_path / "work space"
    cwd.mkdir(exist_ok=True)
    launch = {"argv": ["claude", "--add-dir", "~/.kdcube", "--dangerously-skip-permissions"], "cwd": str(cwd)}
    if tmux:
        launch["tmux_session"] = tmux
    built = build_session_resume_command(
        runtime_kind="claude-code",
        runtime_session_id=SESSION_A,
        launch=launch,
        profile={"config_dir": str(profile), "executable": str(exe)},
    )
    return built, profile, exe


def _run(line: str, *, prelude: str = "", env: dict | None = None) -> str:
    result = subprocess.run(
        ["bash", "-c", f"{prelude}\n{line}"], capture_output=True, text=True, env={**os.environ, **(env or {})},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_resume_line_names_the_profile_and_the_absolute_executable(tmp_path):
    built, profile, exe = _resume(tmp_path, "ana@example.com")
    assert built["argv"][:3] == ["env", f"CLAUDE_CONFIG_DIR={profile}", str(exe)]
    assert built["argv"][3:5] == ["--resume", SESSION_A]
    assert built["profile_dir"] == str(profile)
    assert built["command"].startswith(f"cd {shlex.quote(str(tmp_path / 'work space'))} && env ")


def test_the_resume_line_survives_a_shell_function_and_an_opposite_caller_profile(tmp_path):
    built, profile, _exe = _resume(tmp_path, "ana@example.com")
    # The colleague's routing function, and a caller already in another profile.
    function = 'claude() { CLAUDE_CONFIG_DIR="$HOME/.claude-personal" command claude "$@"; }'
    out = _run(built["command"], prelude=function, env={"CLAUDE_CONFIG_DIR": "/somewhere/else"})
    assert f"dir={profile}" in out
    assert f"arg={SESSION_A}" in out
    assert "arg=--dangerously-skip-permissions" in out


def test_the_tmux_inner_line_is_one_argument_carrying_the_same_profile(tmp_path):
    built, profile, _exe = _resume(tmp_path, "ana@example.com", tmux="ana-mint")
    first, attach = built["command"].split("\n")
    words = shlex.split(first)
    assert words[:5] == ["tmux", "new-session", "-d", "-s", "ana-mint"]
    assert len(words) == 6
    assert attach == "tmux attach -t ana-mint"
    # What tmux hands its shell, run with the same hostile function in place.
    out = _run(words[5], prelude='claude() { CLAUDE_CONFIG_DIR=/wrong command claude "$@"; }')
    assert f"dir={profile}" in out


def test_two_profiles_in_the_same_folder_resume_under_their_own_profiles(tmp_path):
    ana, ana_profile, _ = _resume(tmp_path, "ana@example.com")
    bob, bob_profile, _ = _resume(tmp_path, "bob@example.com")
    assert f"dir={ana_profile}" in _run(ana["command"])
    assert f"dir={bob_profile}" in _run(bob["command"])


@pytest.mark.parametrize("executable", ["claude", "bin/claude", "/usr/bin/sh", "/opt/x/claude\nrm"])
def test_an_executable_that_is_not_an_absolute_claude_is_refused(tmp_path, executable):
    with pytest.raises(DomainError) as caught:
        build_session_resume_command(
            runtime_kind="claude-code",
            runtime_session_id=SESSION_A,
            working_directory=str(tmp_path),
            profile={"config_dir": str(tmp_path / "p"), "executable": executable},
        )
    assert caught.value.code == "work_session_resume_executable_invalid"


def test_an_agent_without_a_profile_gets_no_resume_line(tmp_path):
    # Operator, 2026-10-12: "we re not going to work wit unscoped claude agents."
    with pytest.raises(DomainError) as caught:
        build_session_resume_command(
            runtime_kind="claude-code", runtime_session_id=SESSION_A, working_directory=str(tmp_path), profile=None
        )
    assert caught.value.code == "work_session_resume_profile_required"
    assert cp.resume_profile({"state": cp.UNBOUND})["config_dir"] == ""


def test_codex_lines_never_carry_a_claude_profile(tmp_path):
    built = build_session_resume_command(
        runtime_kind="codex",
        runtime_session_id=SESSION_B,
        working_directory=str(tmp_path),
        profile={"config_dir": str(tmp_path / "p"), "executable": "/opt/claude"},
    )
    assert built["argv"][:3] == ["codex", "resume", SESSION_B]


def test_the_stable_executable_is_the_path_entry_not_its_resolved_version(tmp_path):
    versions = tmp_path / "versions"
    _fake_claude(versions)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "claude").symlink_to(versions / "claude")
    assert cp.claude_executable(str(link_dir)) == str(link_dir / "claude")


# -- skill and settings per profile ----------------------------------------------


def test_installs_go_to_the_sessions_own_profile(tmp_path):
    folder = tmp_path / ".claude-accounts" / "ana@example.com"
    assert claude_config_root(None, environ={"CLAUDE_CONFIG_DIR": str(folder)}) == folder
    assert claude_config_root(tmp_path, environ={"CLAUDE_CONFIG_DIR": str(folder)}) == tmp_path / ".claude"
    assert claude_config_root(tmp_path, str(folder)) == folder
    with pytest.raises(DomainError):
        claude_config_root(None, "relative/dir")


class _OldBoard:
    """A board from before W763: refuses session-profile evidence, accepts the W310 form."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def action(self, *, object_ref, action, payload):
        self.calls.append({"action": action, "evidence": dict(payload.get("runtime_account_evidence") or {})})
        if (payload.get("runtime_account_evidence") or {}).get("source") == "session-profile":
            raise DomainError("work_runtime_account_evidence_invalid", "unsupported fields")
        return {"ok": True}


def _old_board_adapter():
    adapter = SimpleNamespace(client=_OldBoard(), _runtime_account_error_state={}, config=SimpleNamespace(worker_name="w"))
    adapter._account_action = lambda **kw: ProblemBoardHostRelayAdapter._account_action(adapter, **kw)
    return adapter


def test_an_older_board_gets_the_w310_form_and_no_heartbeat_is_lost():
    adapter = _old_board_adapter()
    payload = {
        "runtime_account": {"account_id": ACCOUNT_A},
        "runtime_account_evidence": {
            "state": "reported", "source": "session-profile", "profile_state": "verified", "observed_at": "t",
        },
    }
    assert asyncio.run(adapter._account_action(action="worker.heartbeat", payload=payload)) == {"ok": True}
    assert adapter._runtime_account_error_state["legacy_evidence"] is True
    assert adapter.client.calls[-1]["evidence"] == {"state": "reported", "source": "host-report", "observed_at": "t"}


def test_the_w310_form_of_a_mismatch_still_names_no_account():
    payload = {
        "runtime_account_evidence": {
            "state": "missing", "source": "session-profile", "profile_state": "mismatch", "observed_at": "t",
        },
    }
    from project_board.client.relay import _legacy_account_evidence

    assert _legacy_account_evidence(payload) is True
    assert payload == {"runtime_account_evidence": {"state": "missing", "source": "host-report", "observed_at": "t"}}


def test_other_refusals_are_not_swallowed():
    class Refusing:
        async def action(self, **_):
            raise DomainError("work_worker_unavailable", "gone")

    adapter = SimpleNamespace(client=Refusing(), _runtime_account_error_state={}, config=SimpleNamespace(worker_name="w"))
    with pytest.raises(DomainError) as caught:
        asyncio.run(ProblemBoardHostRelayAdapter._account_action(adapter, action="worker.heartbeat", payload={}))
    assert caught.value.code == "work_worker_unavailable"


def test_the_skill_and_settings_install_into_a_new_profile_folder(tmp_path):
    from project_board.client.claude_settings import merge_claude_code_settings
    from project_board.client.procedures import install_agent_procedure, verify_agent_procedure

    home = tmp_path / "home"
    home.mkdir()
    folder = home / ".claude-accounts" / "ana@example.com"
    installed = install_agent_procedure(["claude-code"], home=home, claude_config_dir=folder)
    assert (folder / "skills" / "problem-board-worker" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills").exists()
    assert installed
    [verified] = verify_agent_procedure(["claude-code"], home=home, claude_config_dir=folder)
    assert verified["state"] == "current"
    merge_claude_code_settings(home, pb="/opt/pb/bin/pb", config_dir=folder)
    settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    assert "limit-state" in settings["statusLine"]["command"]
    assert not (home / ".claude" / "settings.json").exists()


# -- migration of a legacy session -------------------------------------------------

from project_board.client.claude_profile_migration import (  # noqa: E402
    apply_migration,
    plan_migration,
    rollback_migration,
)

OLD = 1_000_000.0  # file times far before "now", so the session counts as stopped


def _legacy_folder(home: Path) -> Path:
    source = home / ".claude"
    project = source / "projects" / "-Users-ana-work"
    (project / SESSION_A / "subagents").mkdir(parents=True)
    (project / f"{SESSION_A}.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    (project / SESSION_A / "subagents" / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
    (project / "memory").mkdir()
    (project / "memory" / "MEMORY.md").write_text("- a memory\n", encoding="utf-8")
    # Another session in the same project folder: never moved with this one.
    (project / f"{SESSION_B}.jsonl").write_text('{"other": 1}\n', encoding="utf-8")
    (source / "file-history" / SESSION_A).mkdir(parents=True)
    (source / "file-history" / SESSION_A / "f1").write_text("v1", encoding="utf-8")
    (source / "todos").mkdir()
    (source / "todos" / f"{SESSION_A}-agent-{SESSION_A}.json").write_text("[]", encoding="utf-8")
    (source / ".credentials.json").write_text('{"secret": "never"}', encoding="utf-8")
    (source / "settings.json").write_text("{}", encoding="utf-8")
    for path in source.rglob("*"):
        os.utime(path, (OLD, OLD))
    return source


def test_a_dry_run_lists_only_the_sessions_own_files_and_writes_nothing(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    plan = plan_migration(source=source, target=target, session_id=SESSION_A)
    paths = {row["path"] for row in plan["files"]}
    assert paths == {
        f"projects/-Users-ana-work/{SESSION_A}.jsonl",
        f"projects/-Users-ana-work/{SESSION_A}/subagents/agent-1.jsonl",
        f"file-history/{SESSION_A}/f1",
        f"todos/{SESSION_A}-agent-{SESSION_A}.json",
    }
    assert plan["blockers"] == []
    assert not target.exists()
    # The project folder's memory only on the operator's word.
    with_memory = plan_migration(source=source, target=target, session_id=SESSION_A, with_memory=True)
    assert "projects/-Users-ana-work/memory/MEMORY.md" in {row["path"] for row in with_memory["files"]}


def test_apply_copies_verifies_and_never_touches_credentials_or_originals(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    result = apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    assert result["verified"] == {"files": 4, "state": "verified"}
    assert (target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").read_text() == '{"turn": 1}\n'
    assert not (target / ".credentials.json").exists()
    assert not (target / "settings.json").exists()
    assert not (target / "projects" / "-Users-ana-work" / f"{SESSION_B}.jsonl").exists()
    assert (source / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").exists()
    # A retry is a no-op that verifies again.
    again = apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    assert again["copy"] == 0 and again["present"] == 4


def test_a_running_session_is_not_migrated(home):
    source = _legacy_folder(home)
    transcript = source / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"
    os.utime(transcript, None)  # written just now
    with pytest.raises(DomainError) as caught:
        apply_migration(
            source=source, target=home / ".claude-accounts" / "ana@example.com", session_id=SESSION_A, session_stopped=True
        )
    assert caught.value.code == "work_claude_profile_migration_blocked"
    assert "session_running" in caught.value.details["blockers"]


def test_a_different_file_already_in_the_profile_stops_everything(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    conflict = target / "projects" / "-Users-ana-work" / "memory" / "MEMORY.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("- a different memory\n", encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True, with_memory=True)
    assert caught.value.details["conflicts"] == ["projects/-Users-ana-work/memory/MEMORY.md"]
    assert not (target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").exists()


def test_rollback_removes_only_what_the_migration_created(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    target.mkdir(parents=True)
    (target / ".claude.json").write_text("{}", encoding="utf-8")  # the profile's own login stays
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    result = rollback_migration(target=target, session_id=SESSION_A)
    assert result["removed"] == 4
    assert (target / ".claude.json").exists()
    assert not (target / "projects").exists()
    assert (source / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").exists()


def test_rollback_refuses_once_the_session_ran_in_the_profile(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    transcript = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"
    transcript.write_text('{"turn": 1}\n{"turn": 2}\n', encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        rollback_migration(target=target, session_id=SESSION_A)
    assert caught.value.code == "work_claude_profile_rollback_conflict"
    assert transcript.exists()


# -- operator commands -------------------------------------------------------------------


def _host_with_session(tmp_path: Path, *, stored_email: str = "", stored_id: str = ""):
    from project_board.client.store import SharedFieldStore

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
    identity = WorkerSessionIdentity.create("claude-code", SESSION_A)
    channel = host_config.enroll_worker_channel(
        host.path, identity=identity, profile="problem-board-claude-one", authorized=True,
        runtime_launch={"argv": ["claude", "--add-dir", "~/.kdcube"], "cwd": str(tmp_path)},
    )
    if stored_email:
        field = SharedFieldStore(host_config.HostRelayConfig.load(host.path).field_root)
        field.register_worker(
            worker_name=channel.worker_name,
            runtime_kind="claude-code",
            capabilities=[],
            authority_label="test",
            worker_identity=channel.worker_identity,
            runtime_session_id=SESSION_A,
        )
        field.record_worker_board_record(
            channel.worker_name, {"runtime_account": {"account_id": stored_id, "email": stored_email}}
        )
    return host.path


def _args(command: str, **values):
    defaults = {"runtime_session_id": SESSION_A, "email": "", "confirm": False, "source": "", "apply": False}
    return SimpleNamespace(claude_profile_command=command, **{**defaults, **values})


def test_status_proposes_the_folder_of_the_stored_email_for_a_legacy_session(tmp_path, home):
    from project_board.client.claude_profile_commands import claude_profile_command

    path = _host_with_session(tmp_path, stored_email="Ana@Example.com", stored_id=ACCOUNT_A)
    status = claude_profile_command(_args("status"), config_path=path, home=home)
    [row] = status["sessions"]
    assert row["profile_state"] == cp.UNBOUND
    assert row["proposed_config_dir"] == str(home / ".claude-accounts" / "ana@example.com")


def test_bind_needs_the_operators_confirmation(tmp_path, home):
    from project_board.client.claude_profile_commands import claude_profile_command

    path = _host_with_session(tmp_path, stored_email="ana@example.com", stored_id=ACCOUNT_A)
    with pytest.raises(DomainError) as caught:
        claude_profile_command(_args("bind", email="ana@example.com"), config_path=path, home=home)
    assert caught.value.code == "work_claude_profile_confirmation_required"


def test_bind_by_stored_email_gives_a_complete_resume_line(tmp_path, home, monkeypatch):
    from project_board.client.claude_profile_commands import claude_profile_command

    monkeypatch.setattr(cp, "claude_executable", lambda path_env=None: "/opt/claude/bin/claude")
    path = _host_with_session(tmp_path, stored_email="ana@example.com", stored_id=ACCOUNT_A)
    result = claude_profile_command(_args("bind", email="ana@example.com", confirm=True), config_path=path, home=home)
    folder = home / ".claude-accounts" / "ana@example.com"
    assert result["bound"]["config_dir"] == str(folder)
    # Not verified until the operator logs that folder in.
    assert result["bound"]["profile_state"] == cp.UNREADABLE
    assert f"CLAUDE_CONFIG_DIR={folder}" in result["resume_command"]
    assert f"/opt/claude/bin/claude --resume {SESSION_A}" in result["resume_command"]
    _login(folder, ACCOUNT_A, "ana@example.com")
    [row] = claude_profile_command(_args("status"), config_path=path, home=home)["sessions"]
    assert row["profile_state"] == cp.VERIFIED


def test_bind_to_an_email_the_board_does_not_know_needs_that_folders_login(tmp_path, home):
    from project_board.client.claude_profile_commands import claude_profile_command

    path = _host_with_session(tmp_path, stored_email="ana@example.com", stored_id=ACCOUNT_A)
    with pytest.raises(DomainError) as caught:
        claude_profile_command(_args("bind", email="bob@example.com", confirm=True), config_path=path, home=home)
    assert caught.value.code == "work_claude_profile_account_unknown"
    _login(home / ".claude-accounts" / "bob@example.com", ACCOUNT_B, "bob@example.com")
    result = claude_profile_command(_args("bind", email="bob@example.com", confirm=True), config_path=path, home=home)
    assert result["bound"]["profile_state"] == cp.VERIFIED


def test_a_running_relay_takes_a_binding_confirmed_later(home):
    folder = _profile(home, "ana@example.com", ACCOUNT_A)
    binding = {"config_dir": str(folder), "account_id": ACCOUNT_A, "executable": "/opt/claude/bin/claude"}
    adapter = SimpleNamespace(
        config=SimpleNamespace(claude_profile={}, runtime_kind="claude-code"),
        _runtime_account_reader=None,
    )
    import project_board.client.relay as relay_module

    original = relay_module.replace
    relay_module.replace = lambda config, **changes: SimpleNamespace(**{**config.__dict__, **changes})
    try:
        assert ProblemBoardHostRelayAdapter.rebind_claude_profile(adapter, binding) is True
        assert ProblemBoardHostRelayAdapter.rebind_claude_profile(adapter, dict(binding, captured_at="x")) is False
    finally:
        relay_module.replace = original
    assert adapter.config.claude_profile["config_dir"] == str(folder)
    sample = asyncio.run(adapter._runtime_account_reader())
    assert sample.profile_state == cp.VERIFIED
    assert sample.account["account_id"] == ACCOUNT_A


def test_a_new_claude_session_without_its_account_folder_does_not_enroll():
    from project_board.client import cli

    # conftest removes CLAUDE_CONFIG_DIR: this is a session on the default folder.
    with pytest.raises(DomainError) as caught:
        cli._require_claude_profile_for_new_session()
    assert caught.value.code == "work_claude_profile_required"
    assert "env CLAUDE_CONFIG_DIR=" in str(caught.value)



def test_nothing_is_copied_without_the_operators_word_that_the_session_stopped(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    with pytest.raises(DomainError) as caught:
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=False)
    assert caught.value.code == "work_claude_profile_session_not_confirmed_stopped"
    assert not target.exists()


def test_a_link_inside_the_sessions_files_stops_the_copy(home, tmp_path):
    source = _legacy_folder(home)
    outside = tmp_path / "outside.txt"
    outside.write_text("not this session's", encoding="utf-8")
    (source / "file-history" / SESSION_A / "linked").symlink_to(outside)
    with pytest.raises(DomainError) as caught:
        plan_migration(source=source, target=home / ".claude-accounts" / "ana@example.com", session_id=SESSION_A)
    assert caught.value.code == "work_claude_profile_path_link"


def test_a_profile_folder_behind_a_link_is_refused(home, tmp_path):
    source = _legacy_folder(home)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (home / ".claude-accounts").mkdir()
    (home / ".claude-accounts" / "ana@example.com").symlink_to(real)
    with pytest.raises(DomainError) as caught:
        apply_migration(
            source=source, target=home / ".claude-accounts" / "ana@example.com", session_id=SESSION_A, session_stopped=True
        )
    assert caught.value.code == "work_claude_profile_path_link"
    assert list(real.iterdir()) == []


def test_an_existing_partial_file_is_never_replaced(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    old = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl.pb-partial"
    old.parent.mkdir(parents=True)
    old.write_text("someone else's", encoding="utf-8")
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    assert old.read_text(encoding="utf-8") == "someone else's"


def test_rollback_after_an_interrupted_copy_removes_its_partial_file(home, monkeypatch):
    import project_board.client.claude_profile_migration as migration

    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    calls = {"n": 0}
    real_copy = migration.shutil.copy2

    def interrupted(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            Path(dst).write_text("half", encoding="utf-8")
            raise KeyboardInterrupt
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(migration.shutil, "copy2", interrupted)
    with pytest.raises(KeyboardInterrupt):
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    manifest = json.loads((target / ".pb-migrations" / f"{SESSION_A}.json").read_text(encoding="utf-8"))
    assert len(manifest["created"]) == 1 and len(manifest["pending"]) == 1
    partial = target / manifest["pending"][0]["partial"]
    assert partial.exists()
    rollback_migration(target=target, session_id=SESSION_A)
    assert not partial.exists()
    assert not (target / "projects").exists()


def test_rollback_refuses_a_manifest_path_outside_the_profile(home):
    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    manifest_path = target / ".pb-migrations" / f"{SESSION_A}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created"].append({"path": "../../.claude/settings.json", "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        rollback_migration(target=target, session_id=SESSION_A)
    assert caught.value.code == "work_claude_profile_path_invalid"
    assert (source / "settings.json").exists()
    assert (target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").exists()


# -- same machine, same account login (operator, 2026-10-12, via the coordinator) ---------

from project_board.client.claude_profile_migration import preserve_login  # noqa: E402

TOKEN = '{"claudeAiOauth": {"accessToken": "test-access-not-real", "refreshToken": "test-refresh-not-real"}}'


def _global_login(home: Path, account_id: str) -> Path:
    source = home / ".claude"
    source.mkdir(exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({
            "oauthAccount": {"accountUuid": account_id, "emailAddress": "ana@example.com"},
            "hasCompletedOnboarding": True,
            "projects": {"/private": {"history": ["not copied"]}},
        }),
        encoding="utf-8",
    )
    (source / ".credentials.json").write_text(TOKEN, encoding="utf-8")
    return source


def test_the_same_accounts_login_is_kept_in_its_profile(home):
    source = _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    result = preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    assert result == {"login": "copied", "account_id": ACCOUNT_A}
    assert TOKEN not in json.dumps(result)
    assert (target / ".credentials.json").read_text(encoding="utf-8") == TOKEN
    assert oct((target / ".credentials.json").stat().st_mode & 0o777) == "0o600"
    seeded = json.loads((target / ".claude.json").read_text(encoding="utf-8"))
    assert seeded == {
        "oauthAccount": {"accountUuid": ACCOUNT_A, "emailAddress": "ana@example.com"},
        "hasCompletedOnboarding": True,
    }
    # The profile now reads as this account.
    assert cp.read_profile_account(target)["account_id"] == ACCOUNT_A
    # Again: nothing changes.
    assert preserve_login(source=source, target=target, account_id=ACCOUNT_A) == {"login": "present"}


def test_another_accounts_login_is_never_copied(home):
    source = _global_login(home, ACCOUNT_B)
    target = home / ".claude-accounts" / "ana@example.com"
    with pytest.raises(DomainError) as caught:
        preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    assert caught.value.code == "work_claude_profile_login_other_account"
    assert not (target / ".credentials.json").exists()


def test_a_profile_with_its_own_login_keeps_it(home):
    source = _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    target.mkdir(parents=True)
    (target / ".credentials.json").write_text("its own", encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    assert caught.value.code == "work_claude_profile_login_exists"
    assert (target / ".credentials.json").read_text(encoding="utf-8") == "its own"


def test_a_keychain_login_asks_for_a_login_instead(home):
    source = _global_login(home, ACCOUNT_A)
    (source / ".credentials.json").unlink()  # macOS keeps it in the Keychain
    with pytest.raises(DomainError) as caught:
        preserve_login(source=source, target=home / ".claude-accounts" / "ana@example.com", account_id=ACCOUNT_A)
    assert caught.value.code == "work_claude_profile_login_not_a_file"


def test_the_credential_never_reaches_the_migration_manifest(home):
    source = _legacy_folder(home)
    _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    manifest = (target / ".pb-migrations" / f"{SESSION_A}.json").read_text(encoding="utf-8")
    assert ".credentials.json" not in manifest and "test-access" not in manifest


# -- recovery defects found in review (coordinator, 2026-10-12) ----------------------------


def test_a_file_that_appears_at_the_destination_mid_copy_is_never_replaced(home, monkeypatch):
    import project_board.client.claude_profile_migration as migration

    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    real_copy = migration.shutil.copy2
    racing = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"

    def copy_then_race(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        if Path(src).name == f"{SESSION_A}.jsonl":
            racing.write_text("written by someone else", encoding="utf-8")
        return result

    monkeypatch.setattr(migration.shutil, "copy2", copy_then_race)
    with pytest.raises(DomainError) as caught:
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    assert caught.value.code == "work_claude_profile_conflict"
    assert racing.read_text(encoding="utf-8") == "written by someone else"
    assert not [p for p in racing.parent.iterdir() if ".pb-partial-" in p.name]


@pytest.mark.parametrize("then", ["retry", "rollback"])
def test_a_crash_right_after_a_file_is_published_keeps_its_ownership(home, monkeypatch, then):
    import project_board.client.claude_profile_migration as migration

    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    real_publish = migration._publish

    def publish_then_crash(partial, destination):
        real_publish(partial, destination)
        raise KeyboardInterrupt  # before the manifest records it

    monkeypatch.setattr(migration, "_publish", publish_then_crash)
    with pytest.raises(KeyboardInterrupt):
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    monkeypatch.setattr(migration, "_publish", real_publish)
    published = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"
    assert published.exists()
    if then == "retry":
        result = apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
        assert result["verified"] == {"files": 4, "state": "verified"}
        manifest = json.loads((target / ".pb-migrations" / f"{SESSION_A}.json").read_text(encoding="utf-8"))
        assert f"projects/-Users-ana-work/{SESSION_A}.jsonl" in {row["path"] for row in manifest["created"]}
        assert manifest["pending"] == []
    else:
        rollback_migration(target=target, session_id=SESSION_A)
        assert not published.exists()
        assert not (target / "projects").exists()


def test_an_interrupted_login_copy_completes_on_retry(home, monkeypatch):
    import project_board.client.claude_profile_migration as migration

    source = _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    real_write = migration._write_new
    calls = {"n": 0}

    def crash_on_second(path, write):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_write(path, write)

    monkeypatch.setattr(migration, "_write_new", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    monkeypatch.setattr(migration, "_write_new", real_write)
    # The credential came first; the retry adds the metadata.
    assert (target / ".credentials.json").exists() and not (target / ".claude.json").exists()
    assert preserve_login(source=source, target=target, account_id=ACCOUNT_A) == {"login": "copied", "account_id": ACCOUNT_A}
    assert cp.read_profile_account(target)["account_id"] == ACCOUNT_A


def test_a_retry_accepts_this_accounts_metadata_written_before_its_credential(home):
    source = _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    target.mkdir(parents=True)
    (target / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT_A}}), encoding="utf-8")
    assert preserve_login(source=source, target=target, account_id=ACCOUNT_A)["login"] == "copied"
    assert (target / ".credentials.json").read_text(encoding="utf-8") == TOKEN


def test_metadata_of_another_account_in_the_profile_stops_the_login_copy(home):
    source = _global_login(home, ACCOUNT_A)
    target = home / ".claude-accounts" / "ana@example.com"
    target.mkdir(parents=True)
    (target / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT_B}}), encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        preserve_login(source=source, target=target, account_id=ACCOUNT_A)
    assert caught.value.code == "work_claude_profile_login_exists"
    assert not (target / ".credentials.json").exists()


def test_rollback_refuses_a_published_file_changed_after_an_interrupted_publication(home, monkeypatch):
    import project_board.client.claude_profile_migration as migration

    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    real_publish = migration._publish

    def publish_then_crash(partial, destination):
        real_publish(partial, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(migration, "_publish", publish_then_crash)
    with pytest.raises(KeyboardInterrupt):
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    published = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"
    published.write_text("the operator changed this", encoding="utf-8")  # same file, new content
    with pytest.raises(DomainError) as caught:
        rollback_migration(target=target, session_id=SESSION_A)
    assert caught.value.code == "work_claude_profile_rollback_conflict"
    assert published.read_text(encoding="utf-8") == "the operator changed this"


def test_equal_bytes_written_by_someone_else_are_never_claimed(home, monkeypatch):
    """A racing file with the very same content is not this migration's file."""

    import project_board.client.claude_profile_migration as migration

    source = _legacy_folder(home)
    target = home / ".claude-accounts" / "ana@example.com"
    racing = target / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl"
    original = (source / "projects" / "-Users-ana-work" / f"{SESSION_A}.jsonl").read_bytes()
    real_publish = migration._publish

    def someone_else_first(partial, destination):
        if destination == racing:
            racing.write_bytes(original)  # identical bytes, another file
        real_publish(partial, destination)

    monkeypatch.setattr(migration, "_publish", someone_else_first)
    with pytest.raises(DomainError) as caught:
        apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    assert caught.value.code == "work_claude_profile_conflict"
    monkeypatch.setattr(migration, "_publish", real_publish)
    # A retry treats it as already present, not as created.
    apply_migration(source=source, target=target, session_id=SESSION_A, session_stopped=True)
    manifest = json.loads((target / ".pb-migrations" / f"{SESSION_A}.json").read_text(encoding="utf-8"))
    assert f"projects/-Users-ana-work/{SESSION_A}.jsonl" not in {row["path"] for row in manifest["created"]}
    # And a rollback leaves it where it is.
    result = rollback_migration(target=target, session_id=SESSION_A)
    assert racing.read_bytes() == original
    assert result["removed"] == 3


def test_a_linked_source_login_record_is_not_used(home, tmp_path):
    source = _global_login(home, ACCOUNT_A)
    real = tmp_path / "elsewhere.json"
    (home / ".claude.json").rename(real)
    (home / ".claude.json").symlink_to(real)
    with pytest.raises(DomainError) as caught:
        preserve_login(source=source, target=home / ".claude-accounts" / "ana@example.com", account_id=ACCOUNT_A)
    assert caught.value.code == "work_claude_profile_path_link"


def test_the_next_steps_promise_a_login_only_when_none_was_kept(tmp_path):
    from project_board.client.claude_profile_commands import _next_after_migration

    kept = " ".join(_next_after_migration(tmp_path, login_kept=True))
    fresh = " ".join(_next_after_migration(tmp_path, login_kept=False))
    assert "login of the account was kept" in kept and "asks for that account's login" not in kept
    assert "asks for that account's login once" in fresh


@pytest.mark.parametrize("retry_first", [False, True])
def test_an_equal_content_racing_file_survives_rollback_with_or_without_a_retry(tmp_path, monkeypatch, retry_first):
    """The coordinator's review probe (2026-10-12), both variants: rollback never deletes it."""

    import os as _os

    import project_board.client.claude_profile_migration as migration

    session = "11111111-2222-4333-8444-555555555555"
    source = tmp_path / "old-profile"
    original = source / "projects" / "synthetic-project" / f"{session}.jsonl"
    original.parent.mkdir(parents=True)
    original.write_bytes(b'{"synthetic": true}\n')
    _os.utime(original, (1, 1))
    target = tmp_path / "new-profile"
    racing = target / original.relative_to(source)
    real_copy = migration.shutil.copy2
    creator = {}

    def copy_then_equal_content_race(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        if Path(src) == original:
            racing.write_bytes(original.read_bytes())
            creator["inode"] = racing.stat().st_ino
        return result

    monkeypatch.setattr(migration.shutil, "copy2", copy_then_equal_content_race)
    with pytest.raises(DomainError) as caught:
        migration.apply_migration(source=source, target=target, session_id=session, session_stopped=True)
    assert caught.value.code == "work_claude_profile_conflict"
    monkeypatch.setattr(migration.shutil, "copy2", real_copy)
    if retry_first:
        migration.apply_migration(source=source, target=target, session_id=session, session_stopped=True)
    migration.rollback_migration(target=target, session_id=session)
    assert racing.exists() and racing.stat().st_ino == creator["inode"]


def test_a_bound_session_started_in_another_folder_attributes_nothing_until_it_returns(home):
    """Coordinator's review: the old folder's login is not the account the session runs under now."""

    folder_a = _profile(home, "ana@example.com", ACCOUNT_A)
    folder_b = _profile(home, "bob@example.com", ACCOUNT_B)
    bound = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_a)}, home=home)
    elsewhere = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_b)}, home=home, existing=bound)
    assert elsewhere["config_dir"] == str(folder_a)  # never switched
    assert cp.profile_state(elsewhere, home=home) == (cp.MISMATCH, {})
    on_default = cp.capture_binding(environ={}, home=home, existing=bound)
    assert cp.profile_state(on_default, home=home) == (cp.MISMATCH, {})
    back = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_a)}, home=home, existing=elsewhere)
    assert "capture" not in back
    assert cp.profile_state(back, home=home)[0] == cp.VERIFIED


def test_a_running_relay_sees_the_session_start_elsewhere_and_come_back(home, monkeypatch):
    import project_board.client.relay as relay_module

    folder_a = _profile(home, "ana@example.com", ACCOUNT_A)
    folder_b = _profile(home, "bob@example.com", ACCOUNT_B)
    bound = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_a)}, home=home)
    elsewhere = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_b)}, home=home, existing=bound)
    adapter = SimpleNamespace(config=SimpleNamespace(claude_profile=dict(bound), runtime_kind="claude-code"))
    monkeypatch.setattr(
        relay_module, "replace", lambda config, **changes: SimpleNamespace(**{**config.__dict__, **changes})
    )
    assert ProblemBoardHostRelayAdapter.rebind_claude_profile(adapter, elsewhere) is True
    sample = asyncio.run(adapter._runtime_account_reader())
    assert (sample.profile_state, sample.account) == (cp.MISMATCH, {})
    back = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(folder_a)}, home=home, existing=elsewhere)
    assert ProblemBoardHostRelayAdapter.rebind_claude_profile(adapter, back) is True
    assert asyncio.run(adapter._runtime_account_reader()).profile_state == cp.VERIFIED


@pytest.mark.parametrize("wrong_start", ["other", "global"])
def test_coordinator_probe_started_elsewhere_is_not_verified(tmp_path, wrong_start):
    """The coordinator's review probe (2026-10-12): a retained binding is not proof of the actual start."""

    home = tmp_path / "home"
    a = _profile(home, "ana@example.com", ACCOUNT_A)
    b = _profile(home, "bob@example.com", ACCOUNT_B)
    first = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(a), "PATH": ""}, home=home)
    env = {"CLAUDE_CONFIG_DIR": str(b), "PATH": ""} if wrong_start == "other" else {"PATH": ""}
    again = cp.capture_binding(environ=env, home=home, existing=first)
    assert again["config_dir"] == str(a)
    assert cp.profile_state(again, home=home) == (cp.MISMATCH, {})


def test_coordinator_probe_capture_change_reaches_a_running_adapter(tmp_path):
    from dataclasses import make_dataclass

    home = tmp_path / "home"
    a = _profile(home, "ana@example.com", ACCOUNT_A)
    b = _profile(home, "bob@example.com", ACCOUNT_B)
    first = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(a), "PATH": ""}, home=home)
    again = cp.capture_binding(environ={"CLAUDE_CONFIG_DIR": str(b), "PATH": ""}, home=home, existing=first)
    Config = make_dataclass("Config", [("runtime_kind", str), ("claude_profile", dict)])
    adapter = SimpleNamespace(config=Config("claude-code", first))
    assert ProblemBoardHostRelayAdapter.rebind_claude_profile(adapter, again)
    assert adapter.config.claude_profile["captured_config_dir"] == str(b)

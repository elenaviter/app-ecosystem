"""`pb worker forget-card` forgets a revoked Card's local record (W1012 point 6).

The operator, 7 Nov 2026 ~02:17Z, verbatim: "Point 6. B". The design review
(codex-infra) fixed its contract: the read-only first step, the absolute
refusal while this machine holds the credential, the bounded store probe
first, the profile from the agent's own channel or a host terminal argument,
and the removal through Connection Hub's guarded retire_revoked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from project_board.client import forget_card as module
from project_board.client import prerequisites
from project_board.contract.errors import DomainError

ENDPOINT = "https://runtime.example.test/mcp"


def _channel(profile="problem-board-claude-1", name="claude-code-aaa", alias="alpha@host", state="active"):
    return SimpleNamespace(profile=profile, worker_name=name, worker_alias=alias, state=state)


def _config(tmp_path, workers):
    return SimpleNamespace(
        endpoint=ENDPOINT,
        target_id="demo-target",
        connection_hub_state_root=tmp_path / "connection-hub",
        workers=workers,
    )


class _Services:
    def __init__(self, record, *, holds=False, retire=None):
        self.record = record
        self.removed = []
        self.profiles = SimpleNamespace(get=lambda name: record if record and name == record.name else None)
        async def present(_profile):
            return holds

        self.oauth_profile_sessions = SimpleNamespace(credential_present_bounded=present)
        outer = self

        class _ProfileService:
            async def retire_revoked(self, name, *, access_id):
                if retire is not None:
                    raise retire
                outer.removed.append((name, access_id))
                return SimpleNamespace(profile=record, dangling_installations=0)

        self.profile_service = _ProfileService()


def _record(name="problem-board-claude-1", access_id="aut_old", endpoint=ENDPOINT, auth_type="oauth"):
    return SimpleNamespace(name=name, access_id=access_id, endpoint=endpoint, auth_type=auth_type)


def _usable(usable=True):
    return prerequisites.Probes(
        system=lambda: "Linux",
        which=lambda _name: "/usr/bin/x",
        run=lambda _command: None,
        user=lambda: "worker",
        os_release=lambda: "ID=ubuntu",
        keyring_state=lambda: (usable, "secretstorage" if usable else "the login keyring is locked"),
        hashlib_state=lambda: (True, "ok"),
    )


SELECTION = ["--config", "/srv/pb targets/second/relay.json"]


def _run(tmp_path, monkeypatch, services, *, workers=None, profile="", identity=None, access_id="", probes=None,
         selection=None, root=True):
    workers = workers if workers is not None else [_channel()]
    config = _config(tmp_path, workers)
    if not root:
        config.connection_hub_state_root = None
    monkeypatch.setattr(module.HostRelayConfig, "load", lambda _path: config)
    monkeypatch.setattr(module, "selection_arguments", lambda _path, *, target_id: list(selection if selection is not None else []))
    return asyncio.run(
        module.forget_card(
            tmp_path / "relay.json",
            profile=profile,
            identity=identity,
            access_id=access_id,
            probes=probes or _usable(),
            build_services=lambda _paths: services,
        )
    )


def test_without_an_access_id_it_only_reads_and_names_the_access_id_to_revoke(tmp_path, monkeypatch):
    services = _Services(_record())
    result = _run(tmp_path, monkeypatch, services, identity=SimpleNamespace(worker_name="claude-code-aaa"))
    assert result["state"] == "read_only" and result["changed"] is False
    assert result["profile"] == "problem-board-claude-1" and result["alias"] == "alpha@host"
    assert result["recorded_access_id"] == "aut_old"
    assert result["credential_on_this_machine"] is False
    assert result["state_root"].endswith("connection-hub")
    assert result["next_command"] == "pb worker forget-card problem-board-claude-1 --access-id aut_old"
    assert result["state_root"] == str(tmp_path / "connection-hub")
    assert "Revoke access_id aut_old in Connection Hub" in result["next"]
    assert services.removed == []


def test_with_the_access_id_it_forgets_through_the_guarded_retire_and_names_the_new_card_step(tmp_path, monkeypatch):
    services = _Services(_record())
    result = _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_old")
    assert services.removed == [("problem-board-claude-1", "aut_old")]
    assert result["state"] == "forgotten" and result["changed"] is True
    assert result["next_command"] == "pb worker authorize problem-board-claude-1 --device"


def test_a_credential_still_on_this_machine_is_refused_absolutely_with_the_replace_card_line(tmp_path, monkeypatch):
    services = _Services(_record(), holds=True)
    for access_id in ("", "aut_old"):
        with pytest.raises(DomainError) as refused:
            _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id=access_id)
        assert refused.value.code == "work_relay_card_credential_present"
        assert refused.value.details["next"] == "pb worker authorize problem-board-claude-1 --device --replace-card"
    assert services.removed == []


def test_a_locked_store_refuses_before_anything_is_read_or_changed(tmp_path, monkeypatch):
    services = _Services(_record())
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_old", probes=_usable(False))
    assert refused.value.code == "credential_store_locked"
    assert "Never reset it" in str(refused.value)
    assert services.removed == []


def test_a_wrong_access_id_names_the_recorded_one_and_changes_nothing(tmp_path, monkeypatch):
    refusal = Exception("mismatch")
    refusal.code = "oauth_profile_access_id_confirmation_required"
    services = _Services(_record(), retire=refusal)
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_other")
    assert refused.value.code == "oauth_profile_access_id_confirmation_required"
    assert "aut_old" in str(refused.value) and "Nothing was changed" in str(refused.value)


def test_a_credential_arriving_during_the_retire_is_reported_with_the_replace_card_line(tmp_path, monkeypatch):
    refusal = Exception("present")
    refusal.code = "oauth_profile_credential_present"
    services = _Services(_record(), retire=refusal)
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_old")
    assert refused.value.code == "work_relay_card_credential_present"


@pytest.mark.parametrize(
    "record, code",
    [
        (None, "profile_not_found"),
        (_record(endpoint="https://elsewhere.example.test/mcp"), "work_relay_profile_endpoint_mismatch"),
        (_record(auth_type="static_bearer"), "work_relay_card_profile_not_oauth"),
    ],
)
def test_records_it_must_not_touch_are_refused(tmp_path, monkeypatch, record, code):
    services = _Services(record)
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_old")
    assert refused.value.code == code
    assert services.removed == []


def test_the_profile_comes_from_the_agents_own_channel_or_a_named_one(tmp_path, monkeypatch):
    workers = [_channel(), _channel(profile="problem-board-codex-2", name="codex-bbb", alias="beta@host")]
    services = _Services(_record(name="problem-board-codex-2"))
    result = _run(tmp_path, monkeypatch, services, workers=workers, identity=SimpleNamespace(worker_name="codex-bbb"))
    assert result["profile"] == "problem-board-codex-2"
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, workers=workers, identity=None)
    assert refused.value.code == "work_relay_card_profile_required"
    assert {"alias": "beta@host", "profile": "problem-board-codex-2"} in refused.value.details["enrolled"]


def test_a_disabled_channel_is_refused(tmp_path, monkeypatch):
    services = _Services(_record())
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, workers=[_channel(state="disabled")], profile="problem-board-claude-1")
    assert refused.value.code == "work_relay_worker_disabled"


def test_the_receipt_names_the_default_store_when_none_is_configured(tmp_path, monkeypatch):
    from connection_hub.caller.paths import StatePaths

    result = _run(tmp_path, monkeypatch, _Services(_record()), profile="problem-board-claude-1", root=False)
    assert result["state_root"] == str(StatePaths.default().root) and result["state_root"]


def test_every_printed_line_keeps_the_selected_target_quoted(tmp_path, monkeypatch):
    # Two targets with the same Card profile name: the printed lines must reach this one.
    read = _run(tmp_path, monkeypatch, _Services(_record()), profile="problem-board-claude-1", selection=SELECTION)
    assert read["next_command"] == (
        "pb worker forget-card problem-board-claude-1 --access-id aut_old --config '/srv/pb targets/second/relay.json'"
    )
    done = _run(tmp_path, monkeypatch, _Services(_record()), profile="problem-board-claude-1", access_id="aut_old",
                selection=SELECTION)
    assert done["next_command"] == (
        "pb worker authorize problem-board-claude-1 --device --config '/srv/pb targets/second/relay.json'"
    )
    with pytest.raises(DomainError) as present:
        _run(tmp_path, monkeypatch, _Services(_record(), holds=True), profile="problem-board-claude-1", selection=SELECTION)
    assert present.value.details["next"] == (
        "pb worker authorize problem-board-claude-1 --device --config '/srv/pb targets/second/relay.json' --replace-card"
    )
    racing = Exception("present")
    racing.code = "oauth_profile_credential_present"
    with pytest.raises(DomainError) as raced:
        _run(tmp_path, monkeypatch, _Services(_record(), retire=racing), profile="problem-board-claude-1",
             access_id="aut_old", selection=SELECTION)
    assert raced.value.details["next"] == present.value.details["next"]
    # The default target adds nothing.
    plain = _run(tmp_path, monkeypatch, _Services(_record()), profile="problem-board-claude-1")
    assert plain["next_command"] == "pb worker forget-card problem-board-claude-1 --access-id aut_old"


def test_an_agent_never_acts_on_another_agents_profile(tmp_path, monkeypatch):
    workers = [_channel(), _channel(profile="problem-board-codex-2", name="codex-bbb", alias="beta@host")]
    services = _Services(_record(name="problem-board-codex-2"))
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, workers=workers, identity=SimpleNamespace(worker_name="claude-code-aaa"),
             profile="problem-board-codex-2", access_id="aut_old")
    assert refused.value.code == "work_relay_card_profile_not_own"
    assert services.removed == []
    # Its own profile, named or not, is fine; a person's host terminal may name any.
    own = _run(tmp_path, monkeypatch, _Services(_record()), workers=workers,
               identity=SimpleNamespace(worker_name="claude-code-aaa"), profile="problem-board-claude-1")
    assert own["profile"] == "problem-board-claude-1"
    host = _run(tmp_path, monkeypatch, services, workers=workers, identity=None, profile="problem-board-codex-2")
    assert host["profile"] == "problem-board-codex-2"
    # An agent session with no channel on this target is refused too.
    with pytest.raises(DomainError) as stranger:
        _run(tmp_path, monkeypatch, services, workers=workers, identity=SimpleNamespace(worker_name="other"),
             profile="problem-board-codex-2")
    assert stranger.value.code == "work_relay_card_profile_required"


def test_an_unexpected_failure_never_echoes_its_text(tmp_path, monkeypatch):
    canary = RuntimeError("CANARY-token-value-must-not-print")
    services = _Services(_record(), retire=canary)
    with pytest.raises(DomainError) as refused:
        _run(tmp_path, monkeypatch, services, profile="problem-board-claude-1", access_id="aut_old")
    assert refused.value.code == "work_relay_card_forget_refused"
    assert "CANARY" not in str(refused.value) and "CANARY" not in repr(refused.value.details)
    assert refused.value.details["cause"] == "RuntimeError"


def test_the_cli_parses_forget_card_with_and_without_a_profile():
    from project_board.client import cli

    parser = cli.build_parser()
    args = parser.parse_args(["worker", "forget-card", "problem-board-claude-1", "--access-id", "aut_old"])
    assert (args.worker_command, args.profile, args.access_id) == ("forget-card", "problem-board-claude-1", "aut_old")
    args = parser.parse_args(["worker", "forget-card", "--runtime-kind", "claude-code", "--runtime-session-id", "s1"])
    assert args.profile == "" and args.access_id == ""


_STUCK_CLI = r"""
import asyncio, pathlib, sys, threading
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from connection_hub.caller.authorization import profile_session
from test_oauth_credential_lock import _authorized
from project_board.client import forget_card as module
from project_board.client import prerequisites
from project_board.contract.errors import DomainError

profile_session.CUSTODY_DEADLINE_SECONDS = 0.5
tmp = pathlib.Path(sys.argv[2])

class Stuck:
    def __init__(self, inner):
        self.inner = inner
    def get(self, ref):
        threading.Event().wait()  # a native store that never answers
    def put(self, ref, token):
        return self.inner.put(ref, token)
    def remove(self, ref):
        return self.inner.remove(ref)

async def main():
    service, profiles, credentials = await _authorized(tmp, ["agent-a"])
    record = profiles.require("agent-a")
    service._credentials = Stuck(credentials)
    channel = SimpleNamespace(profile="agent-a", worker_name="w", worker_alias="a@h", state="active")
    config = SimpleNamespace(endpoint=record.endpoint, target_id="t", connection_hub_state_root=tmp / "hub", workers=[channel])
    module.HostRelayConfig.load = staticmethod(lambda _path: config)
    module.selection_arguments = lambda _path, *, target_id: []
    probes = prerequisites.Probes(system=lambda: "Linux", which=lambda _n: "/usr/bin/x", run=lambda _c: None,
                                  user=lambda: "u", os_release=lambda: "ID=ubuntu", keyring_state=lambda: (True, "ok"),
                                  hashlib_state=lambda: (True, "ok"))
    services = SimpleNamespace(profiles=profiles, oauth_profile_sessions=service, profile_service=None)
    try:
        await module.forget_card(tmp / "relay.json", profile="agent-a", access_id=record.access_id,
                                 probes=probes, build_services=lambda _paths: services)
    except DomainError as exc:
        print(exc.code, flush=True)
    assert profiles.get("agent-a") == record

asyncio.run(main())
print("exited-cleanly", flush=True)
"""


def test_a_store_that_never_answers_refuses_and_the_whole_command_exits(tmp_path):
    import os
    import pathlib
    import subprocess
    import sys
    import time

    hub_tests = pathlib.Path(__file__).resolve().parents[4] / "connection-hub" / "packages" / "connection-hub-cli" / "tests"
    if not (hub_tests / "test_oauth_credential_lock.py").is_file():
        pytest.skip("Connection Hub CLI tests are not beside this package")
    # The release gate installs each package of the pb set in its own venv;
    # connection-hub-cli is not in the set, and its tests import it.
    import importlib.util

    if importlib.util.find_spec("connection_hub_cli") is None:
        pytest.skip("connection_hub_cli is not importable (release gate venv); the source overlay runs this test")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", _STUCK_CLI, str(hub_tests), str(tmp_path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["credential_store_unreadable", "exited-cleanly"]
    assert time.monotonic() - started < 30


def _cli_args(**overrides):
    values = {"worker_command": "forget-card", "profile": "problem-board-codex-2", "access_id": "", "config": None,
              "runtime_kind": "", "runtime_session_id": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_an_explicit_but_invalid_identity_refuses_instead_of_acting_as_a_host_terminal(monkeypatch):
    from project_board.client import cli

    called = []
    monkeypatch.setattr(module, "forget_card", lambda *a, **k: called.append(k))
    for overrides in ({"runtime_kind": "claude-code", "runtime_session_id": "not a valid id"},
                      {"runtime_kind": "claude-code"}):
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "PROBLEM_BOARD_CLAUDE_SESSION_ID",
                     "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(DomainError):
            cli._worker_command(_cli_args(**overrides))
    assert called == []


def test_no_identity_at_all_with_a_named_profile_is_a_host_terminal(monkeypatch, tmp_path):
    from project_board.client import cli

    seen = []

    async def fake(path, **kwargs):
        seen.append(kwargs)
        return {"state": "read_only"}

    monkeypatch.setattr(module, "forget_card", fake)
    monkeypatch.setattr(cli, "resolve_host_config_path", lambda _value: tmp_path / "relay.json")
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "PROBLEM_BOARD_CLAUDE_SESSION_ID",
                 "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli._worker_command(_cli_args()) == {"state": "read_only"}
    assert seen[0]["identity"] is None and seen[0]["profile"] == "problem-board-codex-2"
    # Without a profile, the absent identity still refuses.
    with pytest.raises(DomainError):
        cli._worker_command(_cli_args(profile=""))

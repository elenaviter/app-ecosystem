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
        self.oauth_profile_sessions = SimpleNamespace(credential_present=lambda _profile: holds)
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
        user=lambda: "lena",
        os_release=lambda: "ID=ubuntu",
        keyring_state=lambda: (usable, "secretstorage" if usable else "the login keyring is locked"),
        hashlib_state=lambda: (True, "ok"),
    )


def _run(tmp_path, monkeypatch, services, *, workers=None, profile="", identity=None, access_id="", probes=None):
    workers = workers if workers is not None else [_channel()]
    monkeypatch.setattr(module.HostRelayConfig, "load", lambda _path: _config(tmp_path, workers))
    return asyncio.run(
        module.forget_card(
            tmp_path / "relay.json",
            profile=profile,
            identity=identity,
            access_id=access_id,
            probes=probes or _usable(),
            build_services=lambda _config: services,
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


def test_the_cli_parses_forget_card_with_and_without_a_profile():
    from project_board.client import cli

    parser = cli.build_parser()
    args = parser.parse_args(["worker", "forget-card", "problem-board-claude-1", "--access-id", "aut_old"])
    assert (args.worker_command, args.profile, args.access_id) == ("forget-card", "problem-board-claude-1", "aut_old")
    args = parser.parse_args(["worker", "forget-card", "--runtime-kind", "claude-code", "--runtime-session-id", "s1"])
    assert args.profile == "" and args.access_id == ""

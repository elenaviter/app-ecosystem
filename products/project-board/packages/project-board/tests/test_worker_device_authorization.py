"""Problem Board preserves the RFC 8628 device authorization boundary (W257)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_board.client import authorization, cli
from project_board.contract.errors import DomainError


def _profile(*, access_id: str = "access-one") -> SimpleNamespace:
    return SimpleNamespace(
        name="spark1-worker",
        endpoint="https://runtime.example.test/mcp",
        access_id=access_id,
        auth_type="oauth",
    )


def _probe() -> SimpleNamespace:
    return SimpleNamespace(to_dict=lambda: {"reachable": True})


def _config(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        endpoint="https://runtime.example.test/mcp",
        target_id="demo-target",
        bundle_id="problem-board@1-0",
        host_id="spark1",
        host_label="spark1",
        relay_id="spark1-relay",
        connection_hub_state_root=tmp_path,
    )


def _channel() -> SimpleNamespace:
    return SimpleNamespace(
        worker_alias="spark-worker",
        worker_name="worker-stable-name",
        worker_identity="codex-session",
        runtime_kind="codex",
        runtime_session_id="019fb07f-9251-7f42-a45e-cd212bd5b6c2",
        to_mapping=lambda: {"worker_name": "worker-stable-name"},
    )


def _install_services(monkeypatch, tmp_path, *, existing=None, oauth, profile_service):
    import connection_hub.caller.services as caller_services

    services = SimpleNamespace(
        profiles=SimpleNamespace(get=lambda _name: existing),
        oauth_profile_sessions=oauth,
        profile_service=profile_service,
    )
    monkeypatch.setattr(caller_services, "build_caller_services", lambda **_kwargs: services)
    monkeypatch.setattr(authorization.HostRelayConfig, "load", lambda _path: _config(tmp_path))
    monkeypatch.setattr(authorization, "_matching_channel", lambda *_args: _channel())
    monkeypatch.setattr(
        authorization,
        "_recover_sibling_profile",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        authorization,
        "read_runtime_account",
        AsyncMock(
            return_value={
                "account_id": "vendor-account-one",
                "email": "worker@example.test",
                "organization": "vendor-org-one",
            }
        ),
    )
    return services


def test_worker_authorize_parses_device_mode():
    args = cli.build_parser().parse_args(
        ["worker", "authorize", "spark1-worker", "--device"]
    )
    assert args.profile == "spark1-worker"
    assert args.device is True
    assert args.no_open is False
    assert args.callback_port is None
    assert args.coordinator is False


def test_worker_authorize_parses_coordinator_mode():
    args = cli.build_parser().parse_args(
        ["worker", "authorize", "spark1-worker", "--coordinator"]
    )

    assert args.profile == "spark1-worker"
    assert args.coordinator is True


def test_worker_command_forwards_coordinator_mode(monkeypatch, tmp_path):
    authorize = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(cli, "authorize_worker_profile", authorize)
    monkeypatch.setattr(
        cli,
        "resolve_host_config_path",
        lambda _path: Path(tmp_path / "host.json"),
    )
    args = cli.build_parser().parse_args(
        ["worker", "authorize", "spark1-worker", "--coordinator"]
    )

    assert cli._worker_command(args) == {"ok": True}  # noqa: SLF001
    assert authorize.await_args.kwargs["coordinator"] is True


@pytest.mark.parametrize("options", [{"no_open": True}, {"callback_port": 8765}])
@pytest.mark.asyncio
async def test_device_mode_does_not_accept_loopback_options(options):
    with pytest.raises(DomainError) as raised:
        await authorization.authorize_worker_profile(
            "unused.json",
            profile_name="spark1-worker",
            device=True,
            **options,
        )
    assert raised.value.code == "oauth_device_option_conflict"


def test_device_handoff_prints_only_public_browser_values(capsys):
    authorization._device_authorization_presenter(  # noqa: SLF001 - boundary under test
        SimpleNamespace(
            verification_uri="https://runtime.example.test/device",
            verification_uri_complete=(
                "https://runtime.example.test/device?user_code=BCDF-GHJK"
            ),
            user_code="BCDF-GHJK",
            device_code="private-device-secret",
        )
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "https://runtime.example.test/device" in output
    assert "BCDF-GHJK" in output
    assert "private-device-secret" not in output


@pytest.mark.asyncio
async def test_new_profile_passes_device_mode_to_connection_hub(monkeypatch, tmp_path):
    result = SimpleNamespace(profile=_profile(), probe=_probe())
    oauth = SimpleNamespace(authorize=AsyncMock(return_value=result))
    _install_services(
        monkeypatch,
        tmp_path,
        oauth=oauth,
        profile_service=SimpleNamespace(),
    )

    response = await authorization.authorize_worker_profile(
        "host.json",
        profile_name="spark1-worker",
        device=True,
    )

    kwargs = oauth.authorize.await_args.kwargs
    assert kwargs["device"] is True
    assert kwargs["device_presenter"] is authorization._device_authorization_presenter  # noqa: SLF001
    assert "callback_port" not in kwargs
    assert "browser_opener" not in kwargs
    assert kwargs["scope"] == authorization.WORKER_AUTHORIZATION_PROFILE_SCOPE
    assert "default_scope" not in kwargs
    assert "Problem Board worker" in kwargs["client_name"]
    assert kwargs["client_metadata"]["kdcube_credential_use"] == "multi_resource"
    assert kwargs["client_metadata"]["kdcube_authorization_profile"] == "worker"
    assert kwargs["client_metadata"]["kdcube_agent_account"] == {
        "account_id": "vendor-account-one",
        "email": "worker@example.test",
        "organization": "vendor-org-one",
    }
    assert response["profile"]["access_id"] == "access-one"


@pytest.mark.asyncio
async def test_new_profile_authorizes_when_provider_account_is_not_reported(
    monkeypatch,
    tmp_path,
    capsys,
):
    result = SimpleNamespace(profile=_profile(), probe=_probe())
    oauth = SimpleNamespace(authorize=AsyncMock(return_value=result))
    _install_services(
        monkeypatch,
        tmp_path,
        oauth=oauth,
        profile_service=SimpleNamespace(),
    )
    monkeypatch.setattr(
        authorization,
        "read_runtime_account",
        AsyncMock(
            side_effect=DomainError(
                "work_runtime_account_unavailable",
                "The coding runtime did not report its signed-in account.",
                status=409,
            )
        ),
    )

    response = await authorization.authorize_worker_profile(
        "host.json",
        profile_name="spark1-worker",
        device=True,
    )

    metadata = oauth.authorize.await_args.kwargs["client_metadata"]
    assert "kdcube_agent_account" not in metadata
    assert response["profile"]["access_id"] == "access-one"
    assert (
        "Provider account not reported: work_runtime_account_unavailable."
        in capsys.readouterr().err
    )


@pytest.mark.asyncio
async def test_new_coordinator_profile_requests_only_the_coordinator_marker(
    monkeypatch,
    tmp_path,
):
    result = SimpleNamespace(profile=_profile(), probe=_probe())
    oauth = SimpleNamespace(authorize=AsyncMock(return_value=result))
    _install_services(
        monkeypatch,
        tmp_path,
        oauth=oauth,
        profile_service=SimpleNamespace(),
    )

    await authorization.authorize_worker_profile(
        "host.json",
        profile_name="spark1-worker",
        coordinator=True,
    )

    kwargs = oauth.authorize.await_args.kwargs
    assert kwargs["scope"] == authorization.COORDINATOR_AUTHORIZATION_PROFILE_SCOPE
    assert authorization.WORKER_AUTHORIZATION_PROFILE_SCOPE not in kwargs["scope"]
    assert "Problem Board coordinator" in kwargs["client_name"]
    assert kwargs["client_metadata"]["kdcube_authorization_profile"] == "coordinator"


@pytest.mark.asyncio
async def test_reconnect_passes_device_mode_and_keeps_the_same_card(monkeypatch, tmp_path):
    class LoginRequired(Exception):
        code = "oauth_profile_login_required"

    existing = _profile()
    result = SimpleNamespace(profile=_profile(), probe=_probe())
    oauth = SimpleNamespace(reconnect=AsyncMock(return_value=result))
    profile_service = SimpleNamespace(
        probe_profile=AsyncMock(side_effect=LoginRequired("sign in again"))
    )
    _install_services(
        monkeypatch,
        tmp_path,
        existing=existing,
        oauth=oauth,
        profile_service=profile_service,
    )

    response = await authorization.authorize_worker_profile(
        "host.json",
        profile_name="spark1-worker",
        device=True,
        coordinator=True,
    )

    kwargs = oauth.reconnect.await_args.kwargs
    assert kwargs["device"] is True
    assert kwargs["device_presenter"] is authorization._device_authorization_presenter  # noqa: SLF001
    assert response["profile"]["access_id"] == existing.access_id
    assert response["card_preserved"] is True


@pytest.mark.asyncio
async def test_replace_card_applies_the_selected_profile(monkeypatch, tmp_path):
    existing = _profile()
    replacement = _profile(access_id="access-two")
    oauth = SimpleNamespace(
        authorize=AsyncMock(
            return_value=SimpleNamespace(profile=replacement, probe=_probe())
        )
    )
    profile_service = SimpleNamespace(disconnect=AsyncMock(return_value=None))
    _install_services(
        monkeypatch,
        tmp_path,
        existing=existing,
        oauth=oauth,
        profile_service=profile_service,
    )

    response = await authorization.authorize_worker_profile(
        "host.json",
        profile_name="spark1-worker",
        replace_card=True,
        coordinator=True,
    )

    profile_service.disconnect.assert_awaited_once_with("spark1-worker")
    assert (
        oauth.authorize.await_args.kwargs["scope"]
        == authorization.COORDINATOR_AUTHORIZATION_PROFILE_SCOPE
    )
    assert response["profile"]["access_id"] == "access-two"
    assert response["previous_access_id"] == "access-one"

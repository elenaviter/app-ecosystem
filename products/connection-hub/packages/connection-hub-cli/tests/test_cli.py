from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import anyio
import pytest
import yaml
from connection_hub_cli import cli
from connection_hub_cli.authorization.session import OAuthSessionStore
from connection_hub_cli.clients.adapters import ClaudeDesktopAdapter
from connection_hub_cli.clients.service import ClientService
from connection_hub_cli.errors import CredentialError
from connection_hub_cli.management import (
    DEFAULT_MANAGEMENT_SCOPE,
    ConsentRecovery,
    ExportedSecret,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
    ManagementTarget,
    SecretExportResult,
)
from connection_hub_cli.models import (
    CallerProfile,
    HelperLaunch,
    ProbeResult,
    ProfileOAuthMetadata,
)
from connection_hub_cli.paths import StatePaths
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.state import InstallationStore, ProfileStore


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, credential_ref: str, bearer: str) -> None:
        self.values[credential_ref] = bearer

    def get(self, credential_ref: str) -> str | None:
        return self.values.get(credential_ref)

    def remove(self, credential_ref: str) -> bool:
        return self.values.pop(credential_ref, None) is not None

    def backend_name(self) -> str:
        return "test-keyring"

    def verify_ready(self) -> None:
        return None


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.diagnostic_values: list[dict] = []

    def status(self, *, probe=False):
        return {"selected": False}

    def diagnostics(self, *, probe=False):
        return list(self.diagnostic_values)

    def mcp_endpoint(self):
        return "https://hub.example/mcp"

    def setup_endpoint(self, **kwargs):
        self.calls.append(("setup_endpoint", kwargs))
        return {"changed": True, "target": "endpoint"}

    def setup_existing_local(self, **kwargs):
        self.calls.append(("setup_existing_local", kwargs))
        return {"changed": True, "target": "existing-local"}

    def setup_new_local(self, **kwargs):
        self.calls.append(("setup_new_local", kwargs))
        return {"changed": True, "target": "new-local"}

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"operation": "start", "changed": True}

    def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))
        return {"operation": "stop", "changed": True}

    def open(self):
        self.calls.append(("open", {}))
        return {"operation": "open", "url": "https://hub.example/widget"}

    def management_target(self):
        return ManagementTarget.create(
            public_base_url="https://hub.example",
            tenant="acme",
            project="prod",
        )


class _AuthorizationFlow:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def authorize_and_store(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(session=SimpleNamespace(access_id="access-cli"))


class _ManagementService:
    def __init__(self) -> None:
        self.calls: list[ManagementRequest] = []
        self.results: list[ManagementResult | ManagementDenial] = []

    async def execute(self, request: ManagementRequest):
        self.calls.append(request)
        return self.results.pop(0)

    async def disconnect(self, target_key: str):
        return SimpleNamespace(access_id="access-cli", target_key=target_key)


class _SecretExportService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.marker = "secret-export-marker"

    async def export(self, **kwargs):
        self.calls.append(kwargs)
        return SecretExportResult(
            transaction_id="transaction-a",
            request_digest="b" * 64,
            assurance="session_confirmation",
            approval_method="test_browser_session",
            approval_verified_at=int(time.time()),
            values=tuple(
                ExportedSecret(
                    target=target,
                    value=f"{self.marker}::{target.scope}::{target.key}",
                )
                for target in kwargs["targets"]
            ),
        )


class _OAuthRepository:
    def credential_present(self, _session_id: str) -> bool:
        return True


class _OAuthProfileSessions:
    def __init__(self, profiles: ProfileStore) -> None:
        self.profiles = profiles
        self.calls: list[dict] = []
        self.access_canary = "oauth-access-never-render"
        self.refresh_canary = "oauth-refresh-never-render"

    async def authorize(self, **kwargs):
        self.calls.append(kwargs)
        profile = CallerProfile.create_oauth(
            name=kwargs["name"],
            endpoint=kwargs["endpoint"],
            access_id="access-oauth-agent",
            oauth=ProfileOAuthMetadata(
                protected_resource_metadata_url=(
                    "https://hub.example/.well-known/oauth-protected-resource"
                ),
                resource=kwargs["endpoint"],
                issuer="https://hub.example/oauth",
                token_endpoint="https://hub.example/oauth/token",
                revocation_endpoint="https://hub.example/oauth/revoke",
                client_id=(kwargs.get("client_metadata_url") or "registered-client"),
                client_source=("cimd" if kwargs.get("client_metadata_url") else "dcr"),
                client_metadata_url=kwargs.get("client_metadata_url"),
                scope=kwargs.get("scope") or "mcp",
            ),
        )
        self.profiles.add(profile)
        return SimpleNamespace(
            profile=profile,
            probe=ProbeResult(tool_count=4, server_name="hub", server_version="1"),
        )

    def credential_present(self, _profile: CallerProfile) -> bool:
        return True

    def credential_status(self, _profile: CallerProfile) -> dict[str, object]:
        return {
            "credential": "present",
            "expiry": "current",
            "expires_at": 2_000_000_000,
            "refresh_ready": True,
        }


class _OAuthClientAdapter:
    client = "claude-code"

    def __init__(self) -> None:
        self.entry = None

    def available(self) -> bool:
        return True

    def ensure_mode(self, mode: str) -> None:
        assert mode == "oauth"

    def inspect(self, _server_name: str):
        return self.entry

    def install(self, installation) -> bool:
        self.ensure_mode(installation.mode)
        self.entry = installation.to_entry()
        return True

    def rollback_install(self, _installation) -> None:
        self.entry = None

    def remove(self, _installation) -> bool:
        self.entry = None
        return True

    def authorization_command(self, installation):
        return ["claude", "mcp", "login", installation.server_name]


def _services(tmp_path):
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()

    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=2, server_name="fixture", server_version="1")

    profile_service = ProfileService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        probe=probe,
    )
    client_service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters={},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )
    return cli.Services(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=profile_service,
        client_service=client_service,
        host_service=_Host(),
        oauth_sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        oauth_repository=_OAuthRepository(),
        authorization_flow=_AuthorizationFlow(),
        management_service=_ManagementService(),
        secret_export_service=_SecretExportService(),
        adapters={},
    )


def _management_result(request: ManagementRequest) -> ManagementResult:
    return ManagementResult(
        operation=request.operation,
        resource=request.resource,
        invocation_id=request.invocation_id,
        replay=False,
        authority={"access_id": "access-cli"},
        result={
            "application_id": request.application_id,
            "state": "completed",
            "changed_application_ids": [request.application_id],
            "generation": "generation-2",
        },
    )


def test_direct_oauth_install_does_not_require_a_local_native_store(
    tmp_path,
    monkeypatch,
) -> None:
    def unavailable_store():
        raise CredentialError(
            "unavailable_keyring_backend",
            "Linux Secret Service is unavailable.",
        )

    monkeypatch.setattr(cli, "NativeCredentialStore", unavailable_store)
    services = cli.build_services(paths=StatePaths(tmp_path))
    adapter = _OAuthClientAdapter()
    services.client_service.adapters["claude-code"] = adapter

    result = services.client_service.install(
        client="claude-code",
        endpoint="https://hub.example/mcp",
        mode="oauth",
    )

    assert result.installation.mode == "oauth"
    assert result.authorization_command == (
        "claude",
        "mcp",
        "login",
        "connection-hub-claude-code",
    )
    with pytest.raises(CredentialError) as raised:
        services.credentials.verify_ready()
    assert raised.value.code == "unavailable_keyring_backend"


def test_bridge_install_remains_closed_when_the_native_store_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    def unavailable_store():
        raise CredentialError(
            "unavailable_keyring_backend",
            "Linux Secret Service is unavailable.",
        )

    monkeypatch.setattr(cli, "NativeCredentialStore", unavailable_store)
    services = cli.build_services(paths=StatePaths(tmp_path))
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    services.profiles.add(profile)
    client_config = tmp_path / "claude-desktop.json"
    adapter = ClaudeDesktopAdapter(config_path=client_config)
    services.client_service.adapters["claude-desktop"] = adapter

    with pytest.raises(CredentialError) as raised:
        services.client_service.install(
            client="claude-desktop",
            profile_name="agent",
            mode="bridge",
        )

    assert raised.value.code == "unavailable_keyring_backend"
    assert not client_config.exists()


def _management_denial(
    request: ManagementRequest,
    *,
    expires_at: int | None = None,
) -> ManagementDenial:
    return ManagementDenial(
        status=403,
        code="delegated_request_permit_required",
        retryable=False,
        recovery=ConsentRecovery(
            authorization_url=(
                "https://hub.example/api/integrations/bundles/acme/prod/"
                "connection-hub%401-0/widgets/connections_settings?request=opaque"
            ),
            access_id="access-cli",
            resource=request.resource,
            operation=request.operation,
            application_id=request.application_id,
            invocation_id=request.invocation_id,
            request_digest=request.request_digest or ("a" * 64),
            card_revision=4,
            catalog_version="catalog-7",
            expires_at=(int(time.time()) + 600 if expires_at is None else expires_at),
            choices=("allow_once", "allow_always"),
        ),
    )


def test_profile_add_from_stdin_never_renders_or_persists_the_bearer(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    bearer = "synthetic-bearer-never-render"
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(bearer + "\n"))

    result = cli.main(
        [
            "profile",
            "add",
            "agent",
            "--endpoint",
            "https://hub.example/mcp",
            "--credential-stdin",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert bearer not in captured.out
    assert bearer not in captured.err
    assert bearer not in services.profiles.path.read_text()


def test_profile_add_defaults_to_selected_host_endpoint(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("synthetic-bearer\n"))

    assert cli.main(["profile", "add", "agent", "--credential-stdin"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["endpoint"] == "https://hub.example/mcp"


def test_setup_dispatches_endpoint_existing_and_new_local_targets(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    host = services.host_service
    monkeypatch.setattr(cli, "build_services", lambda: services)

    assert (
        cli.main(
            [
                "setup",
                "--endpoint",
                "https://hub.example",
                "--tenant",
                "acme",
                "--project",
                "prod",
                "--no-open",
            ]
        )
        == 0
    )
    assert host.calls.pop(0) == (
        "setup_endpoint",
        {
            "endpoint": "https://hub.example",
            "tenant": "acme",
            "project": "prod",
            "replace": False,
            "open_browser": False,
        },
    )
    capsys.readouterr()

    existing = tmp_path / "existing"
    assert (
        cli.main(
            [
                "setup",
                "--local-workdir",
                str(existing),
                "--no-start",
                "--no-open",
            ]
        )
        == 0
    )
    operation, values = host.calls.pop(0)
    assert operation == "setup_existing_local"
    assert values["workdir"] == existing
    assert values["start"] is False
    assert values["open_browser"] is False
    capsys.readouterr()

    runtime_root = tmp_path / "runtimes"
    assert (
        cli.main(
            [
                "setup",
                "--runtime-root",
                str(runtime_root),
                "--release",
                "2026.09.02.1429",
                "--google-client-id",
                "client.apps.googleusercontent.com",
                "--bootstrap-admin-email",
                "admin@example.com",
                "--no-start",
                "--no-open",
            ]
        )
        == 0
    )
    operation, values = host.calls.pop(0)
    assert operation == "setup_new_local"
    assert values["runtime_root"] == runtime_root
    assert values["tenant"] == "local"
    assert values["project"] == "connection-hub"
    assert values["release_ref"] == "2026.09.02.1429"
    assert values["auth_type"] == "bundle"
    assert values["google_client_id"] == "client.apps.googleusercontent.com"
    assert values["bootstrap_admin_email"] == "admin@example.com"
    assert values["start"] is False


def test_setup_new_local_requires_google_client_id_when_noninteractive(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())

    result = cli.main(
        [
            "setup",
            "--runtime-root",
            str(tmp_path / "runtimes"),
            "--no-start",
            "--no-open",
        ]
    )

    assert result == 2
    assert "google_client_id_required" in capsys.readouterr().err
    assert services.host_service.calls == []


def test_setup_new_local_allows_explicit_development_auth(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    host = services.host_service
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "setup",
            "--runtime-root",
            str(tmp_path / "runtimes"),
            "--auth",
            "simple",
            "--no-start",
            "--no-open",
        ]
    )

    assert result == 0
    operation, values = host.calls.pop(0)
    assert operation == "setup_new_local"
    assert values["auth_type"] == "simple"
    assert values["google_client_id"] is None
    assert values["bootstrap_admin_email"] is None
    capsys.readouterr()


def test_setup_new_local_prompts_for_default_google_login(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    host = services.host_service
    answers = iter(["client.apps.googleusercontent.com", "admin@example.com"])
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    result = cli.main(
        [
            "setup",
            "--runtime-root",
            str(tmp_path / "runtimes"),
            "--no-start",
            "--no-open",
        ]
    )

    assert result == 0
    operation, values = host.calls.pop(0)
    assert operation == "setup_new_local"
    assert values["auth_type"] == "bundle"
    assert values["google_client_id"] == "client.apps.googleusercontent.com"
    assert values["bootstrap_admin_email"] == "admin@example.com"
    capsys.readouterr()


def test_setup_endpoint_requires_route_coordinates(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(["setup", "--endpoint", "https://hub.example"])

    captured = capsys.readouterr()
    assert result == 2
    assert "endpoint_coordinates_required" in captured.err
    assert services.host_service.calls == []


def test_setup_rejects_options_that_would_be_ignored(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    endpoint_result = cli.main(
        [
            "setup",
            "--endpoint",
            "https://hub.example",
            "--tenant",
            "acme",
            "--project",
            "prod",
            "--release",
            "2026.09.02.1429",
        ]
    )
    assert endpoint_result == 2
    assert "source_selector_not_applicable" in capsys.readouterr().err

    endpoint_auth_result = cli.main(
        [
            "setup",
            "--endpoint",
            "https://hub.example",
            "--tenant",
            "acme",
            "--project",
            "prod",
            "--auth",
            "google",
            "--google-client-id",
            "client.apps.googleusercontent.com",
        ]
    )
    assert endpoint_auth_result == 2
    assert "source_selector_not_applicable" in capsys.readouterr().err

    local_result = cli.main(
        [
            "setup",
            "--local-workdir",
            str(tmp_path / "existing"),
            "--tenant",
            "ignored",
        ]
    )
    assert local_result == 2
    assert "existing_local_coordinates_not_applicable" in capsys.readouterr().err
    assert services.host_service.calls == []


def test_cli_sanitizes_unexpected_failures(monkeypatch, capsys) -> None:
    secret = "synthetic-secret-in-exception"

    def fail():
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "build_services", fail)
    result = cli.main(["status"])
    captured = capsys.readouterr()

    assert result == 1
    assert secret not in captured.err
    assert "internal_error" in captured.err


def test_parser_has_no_argument_that_places_a_bearer_on_argv() -> None:
    help_text = cli.build_parser().format_help()
    assert "--credential " not in help_text
    assert "--bearer" not in help_text


def test_secret_parser_puts_domain_before_selected_host() -> None:
    args = cli.build_parser().parse_args(
        [
            "secrets",
            "host",
            "metadata",
            "services.brave.api_key",
            "--scope",
            "platform",
        ]
    )

    assert args.command == "secrets"
    assert args.secrets_target == "host"
    assert args.secret_command == "metadata"

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "host",
                "secret",
                "metadata",
                "services.brave.api_key",
                "--scope",
                "platform",
            ]
        )


def test_generic_client_command_contains_no_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    services = _services(tmp_path)
    bearer = "synthetic-bearer-never-render"

    async def add_profile() -> None:
        await services.profile_service.add(
            name="agent",
            endpoint="https://hub.example/mcp",
            bearer=bearer,
        )

    anyio.run(add_profile)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(["client", "command", "--profile", "agent"])

    captured = capsys.readouterr()
    assert result == 0
    assert bearer not in captured.out
    assert json.loads(captured.out) == {
        "args": ["mcp", "serve", "--profile", "agent"],
        "command": "/opt/tools/connection-hub",
    }


def test_status_doctor_replace_and_remove_never_render_credentials(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    original = "synthetic-original-bearer"
    replacement = "synthetic-replacement-bearer"

    async def add_profile() -> None:
        await services.profile_service.add(
            name="agent",
            endpoint="https://hub.example/mcp",
            bearer=original,
        )

    anyio.run(add_profile)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    assert cli.main(["status"]) == 0
    assert cli.main(["doctor", "--probe", "--json"]) == 0
    assert cli.main(["profile", "status", "agent", "--probe"]) == 0
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(replacement + "\n"))
    assert (
        cli.main(
            [
                "profile",
                "credential",
                "replace",
                "agent",
                "--credential-stdin",
            ]
        )
        == 0
    )
    assert cli.main(["profile", "remove", "agent"]) == 0

    captured = capsys.readouterr()
    assert original not in captured.out
    assert original not in captured.err
    assert replacement not in captured.out
    assert replacement not in captured.err


def test_doctor_renders_host_failures_and_returns_failure(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    services.host_service.diagnostic_values = [
        {
            "code": "host_surfaces_unreachable",
            "severity": "error",
            "summary": "The selected application routes are unavailable.",
            "recovery": "Start or repair the selected application host.",
        }
    ]
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(["doctor"])

    captured = capsys.readouterr()
    assert result == 1
    assert "host_surfaces_unreachable" in captured.out
    assert "Start or repair" in captured.out


def test_credential_and_profile_commands_report_running_process_boundary(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)

    async def add_profile() -> None:
        await services.profile_service.add(
            name="agent",
            endpoint="https://hub.example/mcp",
            bearer="synthetic-original-bearer",
        )

    anyio.run(add_profile)
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO("synthetic-replacement-bearer\n"),
    )

    assert (
        cli.main(
            [
                "profile",
                "credential",
                "replace",
                "agent",
                "--credential-stdin",
            ]
        )
        == 0
    )
    replacement = json.loads(capsys.readouterr().out)
    assert replacement["client_restart_required"] is True

    assert cli.main(["profile", "remove", "agent"]) == 0
    removal = json.loads(capsys.readouterr().out)
    assert removal["running_helper_stopped"] is False
    assert removal["server_card_revoked"] is False


def test_client_commands_report_reload_and_running_process_boundary(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)

    async def add_profile() -> None:
        await services.profile_service.add(
            name="agent",
            endpoint="https://hub.example/mcp",
            bearer="synthetic-bearer",
        )

    anyio.run(add_profile)
    services.client_service.adapters["claude-desktop"] = ClaudeDesktopAdapter(
        config_path=tmp_path / "claude_desktop_config.json"
    )
    monkeypatch.setattr(cli, "build_services", lambda: services)

    assert cli.main(["client", "install", "claude-desktop", "--profile", "agent"]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["client_reload_may_be_required"] is True

    assert (
        cli.main(
            [
                "client",
                "remove",
                "claude-desktop",
                "connection-hub-agent",
            ]
        )
        == 0
    )
    removed = json.loads(capsys.readouterr().out)
    assert removed["running_helper_stopped"] is False
    assert removed["server_card_revoked"] is False


def test_profile_authorize_reports_oauth_state_without_tokens(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    oauth_profiles = _OAuthProfileSessions(services.profiles)
    services.oauth_profile_sessions = oauth_profiles
    services.profile_service.oauth_sessions = oauth_profiles
    services.client_service.oauth_sessions = oauth_profiles
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "profile",
            "authorize",
            "agent",
            "--endpoint",
            "https://hub.example/mcp",
            "--client-metadata-url",
            "https://client.example/oauth/metadata.json",
            "--callback-port",
            "9124",
            "--wait-seconds",
            "5",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert oauth_profiles.access_canary not in captured.out
    assert oauth_profiles.refresh_canary not in captured.out
    payload = json.loads(captured.out)
    assert payload["profile"]["auth_type"] == "oauth"
    assert payload["profile"]["access_id"] == "access-oauth-agent"
    assert payload["profile"]["refresh_ready"] is True
    assert oauth_profiles.calls[0]["callback_port"] == 9124


def test_client_oauth_install_reports_native_login_command_without_local_profile(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    adapter = _OAuthClientAdapter()
    services.client_service.adapters["claude-code"] = adapter
    services.adapters["claude-code"] = adapter
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "client",
            "install",
            "claude-code",
            "--mode",
            "oauth",
            "--endpoint",
            "https://hub.example/mcp",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_mode"] == "oauth"
    assert payload["selected_mode"] == "oauth"
    assert payload["installation"]["profile"] is None
    assert payload["authorization_command"] == [
        "claude",
        "mcp",
        "login",
        "connection-hub-claude-code",
    ]


def test_host_authorize_uses_selected_target_and_renders_no_credential(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "host",
            "authorize",
            "--client-id",
            "provisioned-client",
            "--wait-seconds",
            "5",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "access_id": "access-cli",
        "authorized": True,
        "credential": "stored",
        "target": {
            "project": "prod",
            "resource": "urn:kdcube:management:deployment:acme:prod",
            "tenant": "acme",
        },
    }
    call = services.authorization_flow.calls[0]
    assert call["target_key"] == "endpoint:https://hub.example:acme:prod"
    assert call["provisioned_client_id"] == "provisioned-client"
    assert call["scope"] == DEFAULT_MANAGEMENT_SCOPE


def test_host_authorize_can_print_manual_browser_url(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)

    async def authorize_and_store(**kwargs):
        opener = kwargs["browser_opener"]
        assert opener("https://hub.example/oauth/authorize?state=public-state") is True
        return SimpleNamespace(session=SimpleNamespace(access_id="access-cli"))

    services.authorization_flow.authorize_and_store = authorize_and_store
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(["host", "authorize", "--no-open", "--wait-seconds", "5"])

    assert result == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["authorized"] is True
    assert "Open this authorization URL in a browser:" in captured.err
    assert "state=public-state" in captured.err


def test_host_disconnect_revokes_before_reporting_local_removal(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(["host", "disconnect"])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "access_id": "access-cli",
        "disconnected": True,
        "local_credential_removed": True,
        "server_card_revoked": True,
        "target": {
            "project": "prod",
            "resource": "urn:kdcube:management:deployment:acme:prod",
            "tenant": "acme",
        },
    }


def test_noninteractive_reload_returns_request_bound_recovery(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    target = services.host_service.management_target()
    request = ManagementRequest.reload(
        target,
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    services.management_service.results = [_management_denial(request)]
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "host",
            "reload",
            "connection-hub@1-0",
            "--invocation-id",
            "reload-1",
            "--no-open",
        ]
    )

    assert result == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovery"]["invocation_id"] == "reload-1"
    assert payload["recovery"]["request_digest"] == request.request_digest
    assert payload["recovery"]["authorization_url"].startswith("https://hub.example/")


def test_interactive_reload_retries_the_same_request_after_browser_approval(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    target = services.host_service.management_target()
    request = ManagementRequest.reload(
        target,
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    services.management_service.results = [
        _management_denial(request),
        _management_result(request),
    ]
    opened: list[str] = []
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) is None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    result = cli.main(
        [
            "host",
            "reload",
            "connection-hub@1-0",
            "--invocation-id",
            "reload-1",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert opened == [
        (
            "https://hub.example/api/integrations/bundles/acme/prod/"
            "connection-hub%401-0/widgets/connections_settings?request=opaque"
        )
    ]
    assert len(services.management_service.calls) == 2
    assert services.management_service.calls[0] is services.management_service.calls[1]


def test_expired_reload_recovery_never_opens_or_retries(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    target = services.host_service.management_target()
    request = ManagementRequest.reload(
        target,
        application_id="connection-hub@1-0",
        invocation_id="reload-expired",
    )
    services.management_service.results = [
        _management_denial(request, expires_at=int(time.time()) - 1)
    ]
    opened: list[str] = []
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) is None)

    result = cli.main(
        [
            "host",
            "reload",
            "connection-hub@1-0",
            "--invocation-id",
            "reload-expired",
        ]
    )

    assert result == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovery"]["expires_at"] < int(time.time())
    assert opened == []
    assert services.management_service.calls == [request]


def test_secret_set_reads_exact_stdin_without_rendering_value(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    target = services.host_service.management_target()
    request = ManagementRequest.secret_write(
        target,
        scope="bundle",
        bundle_id="connection-hub@1-0",
        key="provider.api_key",
        value="secret-input-marker\n",
        invocation_id="secret-write-1",
    )
    services.management_service.results = [
        ManagementResult(
            operation=request.operation,
            resource=request.resource,
            invocation_id=request.invocation_id,
            replay=False,
            authority={"access_id": "access-cli"},
            result={
                "scope": "bundle",
                "bundle_id": "connection-hub@1-0",
                "key": "provider.api_key",
                "state": "stored",
                "created": True,
                "provider": "secrets-service",
            },
        )
    ]
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("secret-input-marker\n"))

    result = cli.main(
        [
            "secrets",
            "host",
            "set",
            "provider.api_key",
            "--scope",
            "bundle",
            "--bundle-id",
            "connection-hub@1-0",
            "--value-stdin",
            "--invocation-id",
            "secret-write-1",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "secret-input-marker" not in captured.out
    assert "secret-input-marker" not in captured.err
    assert services.management_service.calls == [request]
    assert services.management_service.calls[0].request_digest == ""
    assert services.management_service.calls[0].body["value"] == (
        "secret-input-marker\n"
    )


def test_secret_get_writes_private_file_and_never_renders_value(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    output = tmp_path / "provider.secret"
    target = services.host_service.management_target()
    request = ManagementRequest.secret_read(
        target,
        scope="platform",
        key="provider.api_key",
        invocation_id="secret-read-1",
    )
    services.management_service.results = [
        ManagementResult(
            operation=request.operation,
            resource=request.resource,
            invocation_id=request.invocation_id,
            replay=False,
            authority={"access_id": "access-cli"},
            result={
                "scope": "platform",
                "key": "provider.api_key",
                "value": "secret-output-marker",
            },
        )
    ]
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "secrets",
            "host",
            "get",
            "provider.api_key",
            "--scope",
            "platform",
            "--output",
            str(output),
            "--invocation-id",
            "secret-read-1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert output.read_text() == "secret-output-marker"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "secret-output-marker" not in captured.out
    assert "secret-output-marker" not in captured.err
    assert payload["result"]["output"] == str(output.absolute())
    assert payload["result"]["disclosed"] is True
    assert payload["result"]["permissions"] == {"file_mode": "0600"}


def test_secret_get_rejects_existing_output_before_disclosure(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    output = tmp_path / "provider.secret"
    output.write_text("existing")
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "secrets",
            "host",
            "get",
            "provider.api_key",
            "--scope",
            "platform",
            "--output",
            str(output),
            "--invocation-id",
            "secret-read-must-not-disclose",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "secret_output_exists" in captured.err
    assert output.read_text() == "existing"
    assert services.management_service.calls == []


def test_secret_denial_renders_server_bound_recovery_without_value_digest(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    request = ManagementRequest.secret_write(
        services.host_service.management_target(),
        scope="platform",
        key="provider.api_key",
        value="secret-denial-marker",
        invocation_id="secret-write-denied-1",
    )
    services.management_service.results = [_management_denial(request)]
    monkeypatch.setattr(cli, "build_services", lambda: services)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("secret-denial-marker"))

    result = cli.main(
        [
            "secrets",
            "host",
            "set",
            "provider.api_key",
            "--scope",
            "platform",
            "--value-stdin",
            "--invocation-id",
            "secret-write-denied-1",
            "--no-open",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 3
    assert "request_digest" not in {
        key: value for key, value in payload.items() if key != "recovery"
    }
    assert payload["recovery"]["request_digest"] == "a" * 64
    assert "secret-denial-marker" not in captured.out
    assert "secret-denial-marker" not in captured.err


def test_human_secret_export_writes_descriptor_pair_without_rendering_values(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    output = tmp_path / "exported-descriptors"
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "secrets",
            "host",
            "export",
            "--platform-key",
            "services.brave.api_key",
            "--bundle-key",
            "connection-hub@1-0=connections.oauth_state_secret",
            "--output-directory",
            str(output),
            "--wait-seconds",
            "5",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert services.secret_export_service.marker not in captured.out
    assert services.secret_export_service.marker not in captured.err
    payload = json.loads(captured.out)
    assert payload["request_digest"] == "b" * 64
    assert payload["approval"]["verified_at"] <= int(time.time())
    assert payload["output"]["platform_secret_count"] == 1
    assert payload["output"]["bundle_secret_count"] == 1
    platform = yaml.safe_load((output / "secrets.yaml").read_text())
    bundles = yaml.safe_load((output / "bundles.secrets.yaml").read_text())
    assert platform == {
        "services": {
            "brave": {
                "api_key": "secret-export-marker::platform::services.brave.api_key"
            }
        }
    }
    assert bundles == {
        "bundles": {
            "version": "1",
            "items": [
                {
                    "id": "connection-hub@1-0",
                    "secrets": {
                        "connections": {
                            "oauth_state_secret": (
                                "secret-export-marker::bundle::"
                                "connections.oauth_state_secret"
                            )
                        }
                    },
                }
            ],
        }
    }
    assert len(services.secret_export_service.calls) == 1
    assert services.management_service.calls == []
    if cli.sys.platform != "win32":
        assert output.stat().st_mode & 0o777 == 0o700
        assert (output / "secrets.yaml").stat().st_mode & 0o777 == 0o600
        assert (output / "bundles.secrets.yaml").stat().st_mode & 0o777 == 0o600


def test_human_secret_export_preflights_output_before_browser(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    services = _services(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    monkeypatch.setattr(cli, "build_services", lambda: services)

    result = cli.main(
        [
            "secrets",
            "host",
            "export",
            "--platform-key",
            "services.brave.api_key",
            "--output-directory",
            str(output),
        ]
    )

    assert result == 2
    assert "secret_export_output_exists" in capsys.readouterr().err
    assert services.secret_export_service.calls == []

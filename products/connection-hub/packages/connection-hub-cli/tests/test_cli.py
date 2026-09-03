from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import anyio

from connection_hub_cli import cli
from connection_hub_cli.authorization.session import OAuthSessionStore
from connection_hub_cli.clients.adapters import ClaudeDesktopAdapter
from connection_hub_cli.clients.service import ClientService
from connection_hub_cli.management import (
    DEFAULT_MANAGEMENT_SCOPE,
    ConsentRecovery,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
    ManagementTarget,
)
from connection_hub_cli.models import HelperLaunch, ProbeResult
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


class _OAuthRepository:
    def credential_present(self, _session_id: str) -> bool:
        return True


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
        adapters={},
    )


def _management_result(request: ManagementRequest) -> ManagementResult:
    return ManagementResult(
        operation=request.operation,
        resource=request.target.resource,
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
            resource=request.target.resource,
            operation=request.operation,
            application_id=request.application_id,
            invocation_id=request.invocation_id,
            request_digest=request.request_digest,
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

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from kdcube_cli.control import DEFAULT_RUNTIME_ROOT, LocalPlatformSourceRequest

from connection_hub_cli import __version__
from connection_hub_cli.authorization import (
    BrowserAuthorizationFlow,
    HttpxOAuthTransport,
    McpOAuthEndpointDiscovery,
    NativeOAuthProfileCredentialStore,
    NativeOAuthSessionCredentialStore,
    OAuthClient,
    OAuthDiscovery,
    OAuthSessionRepository,
    OAuthSessionStore,
    UnavailableOAuthCredentialStore,
)
from connection_hub_cli.authorization.profile_session import (
    OAuthProfileSessionService,
)
from connection_hub_cli.clients import ClientService, build_client_adapters
from connection_hub_cli.credentials import (
    CredentialStore,
    NativeCredentialStore,
    UnavailableCredentialStore,
)
from connection_hub_cli.diagnostics import collect_diagnostics
from connection_hub_cli.errors import ConnectionHubCliError, CredentialError
from connection_hub_cli.host import HostService
from connection_hub_cli.management import (
    DEFAULT_MANAGEMENT_SCOPE,
    AuthorizedManagementService,
    HttpxManagementTransport,
    ManagementClient,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
)
from connection_hub_cli.mcp_relay import serve_profile
from connection_hub_cli.models import SUPPORTED_CLIENTS
from connection_hub_cli.paths import StatePaths, resolve_helper_launch
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.remote_mcp import probe_remote_tools
from connection_hub_cli.state import HostStore, InstallationStore, ProfileStore


@dataclass(slots=True)
class Services:
    profiles: ProfileStore
    installations: InstallationStore
    credentials: CredentialStore
    profile_service: ProfileService
    client_service: ClientService
    host_service: HostService
    oauth_sessions: OAuthSessionStore
    oauth_repository: OAuthSessionRepository
    authorization_flow: BrowserAuthorizationFlow
    management_service: AuthorizedManagementService
    adapters: dict[str, Any]
    oauth_profile_sessions: OAuthProfileSessionService | None = None


def build_services(*, paths: StatePaths | None = None) -> Services:
    selected_paths = paths or StatePaths.default()
    profiles = ProfileStore(selected_paths.profiles)
    installations = InstallationStore(selected_paths.installations)
    hosts = HostStore(selected_paths.host)
    oauth_sessions = OAuthSessionStore(selected_paths.oauth_sessions)
    try:
        native_credentials = NativeCredentialStore()
    except CredentialError as exc:
        credentials: CredentialStore = UnavailableCredentialStore(exc)
        oauth_credentials = UnavailableOAuthCredentialStore(
            exc,
            error_prefix="oauth_session",
        )
        oauth_profile_credentials = UnavailableOAuthCredentialStore(
            exc,
            error_prefix="oauth_profile",
        )
    else:
        credentials = native_credentials
        oauth_credentials = NativeOAuthSessionCredentialStore(
            backend=native_credentials.native_backend,
            platform_name=native_credentials.platform_name,
        )
        oauth_profile_credentials = NativeOAuthProfileCredentialStore(
            backend=native_credentials.native_backend,
            platform_name=native_credentials.platform_name,
        )
    adapters = build_client_adapters()
    host_service = HostService(store=hosts)
    oauth_transport = HttpxOAuthTransport()
    oauth_discovery = OAuthDiscovery(transport=oauth_transport)
    oauth_client = OAuthClient(transport=oauth_transport)
    oauth_repository = OAuthSessionRepository(
        sessions=oauth_sessions,
        credentials=oauth_credentials,
    )
    authorization_flow = BrowserAuthorizationFlow(
        discovery=oauth_discovery,
        client=oauth_client,
        sessions=oauth_repository,
    )
    oauth_profile_sessions = OAuthProfileSessionService(
        profiles=profiles,
        credentials=oauth_profile_credentials,
        endpoint_discovery=McpOAuthEndpointDiscovery(
            transport=oauth_transport,
        ),
        discovery=oauth_discovery,
        authorization=authorization_flow,
        oauth=oauth_client,
        probe=probe_remote_tools,
    )
    profile_service = ProfileService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        probe=probe_remote_tools,
        oauth_sessions=oauth_profile_sessions,
    )
    client_service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters=adapters,
        launch=resolve_helper_launch(),
        oauth_sessions=oauth_profile_sessions,
    )
    management_service = AuthorizedManagementService(
        sessions=oauth_repository,
        discovery=oauth_discovery,
        oauth=oauth_client,
        management=ManagementClient(transport=HttpxManagementTransport()),
    )
    return Services(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=profile_service,
        client_service=client_service,
        host_service=host_service,
        oauth_sessions=oauth_sessions,
        oauth_repository=oauth_repository,
        oauth_profile_sessions=oauth_profile_sessions,
        authorization_flow=authorization_flow,
        management_service=management_service,
        adapters=adapters,
    )


def _credential_from_input(*, stdin: bool, prompt: str) -> str:
    if stdin:
        value = sys.stdin.readline()
        if value == "":
            raise ConnectionHubCliError(
                "credential_input_missing",
                "No delegated caller credential was provided on standard input.",
            )
        return value
    return getpass.getpass(prompt)


def _new_local_auth(args: argparse.Namespace) -> tuple[str, str | None, str | None]:
    selected = str(args.auth or "google").strip().lower()
    client_id = str(args.google_client_id or "").strip()
    admin_email = str(args.bootstrap_admin_email or "").strip()
    if selected == "simple":
        if client_id or args.bootstrap_admin_email is not None:
            raise ConnectionHubCliError(
                "google_auth_option_not_applicable",
                "Google client and administrator values apply only to Google login.",
            )
        return "simple", None, None

    if not client_id:
        if not sys.stdin.isatty():
            raise ConnectionHubCliError(
                "google_client_id_required",
                "Google login requires --google-client-id when setup is not running in an interactive terminal.",
            )
        client_id = input("Google OAuth client ID (Web application): ").strip()
    if not client_id:
        raise ConnectionHubCliError(
            "google_client_id_required",
            "Google login requires a Google OAuth client ID.",
        )
    if args.bootstrap_admin_email is None and sys.stdin.isatty():
        admin_email = input(
            "Bootstrap administrator email (verified Google email; blank to skip): "
        ).strip()
    return "bundle", client_id, admin_email or None


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _print_manual_authorization_url(url: str) -> bool:
    sys.stderr.write("Open this authorization URL in a browser:\n")
    sys.stderr.write(f"{url}\n")
    sys.stderr.flush()
    return True


def _profile_view(services: Services, profile: Any) -> dict[str, Any]:
    value = {
        "name": profile.name,
        "endpoint": profile.endpoint,
        "access_id": profile.access_id,
        "auth_type": profile.auth_type,
        "credential": "present"
        if services.profile_service.credential_present(profile)
        else "missing",
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }
    if profile.auth_type == "oauth":
        if services.oauth_profile_sessions is None:
            raise ConnectionHubCliError(
                "oauth_profiles_unavailable",
                "OAuth-backed caller profiles are unavailable in this process.",
            )
        value.update(services.oauth_profile_sessions.credential_status(profile))
        value["oauth"] = {
            "resource": profile.oauth.resource,
            "issuer": profile.oauth.issuer,
            "scope": profile.oauth.scope,
            "client_source": profile.oauth.client_source,
        }
    return value


async def _run_profile(args: argparse.Namespace, services: Services) -> int:
    if args.profile_command == "add":
        endpoint = args.endpoint or services.host_service.mcp_endpoint()
        bearer = _credential_from_input(
            stdin=args.credential_stdin,
            prompt="Delegated caller bearer: ",
        )
        profile, result = await services.profile_service.add(
            name=args.name,
            endpoint=endpoint,
            bearer=bearer,
            access_id=args.access_id,
        )
        _print_json(
            {"profile": _profile_view(services, profile), "probe": result.to_dict()}
        )
        return 0

    if args.profile_command == "authorize":
        if services.oauth_profile_sessions is None:
            raise ConnectionHubCliError(
                "oauth_profiles_unavailable",
                "OAuth-backed caller profiles are unavailable in this process.",
            )
        endpoint = args.endpoint or services.host_service.mcp_endpoint()
        authorization_options: dict[str, Any] = {}
        if args.no_open:
            authorization_options["browser_opener"] = _print_manual_authorization_url
        result = await services.oauth_profile_sessions.authorize(
            name=args.name,
            endpoint=endpoint,
            scope=args.scope,
            provisioned_client_id=args.client_id,
            client_metadata_url=args.client_metadata_url,
            callback_port=args.callback_port,
            timeout_seconds=args.wait_seconds,
            **authorization_options,
        )
        _print_json(
            {
                "authorized": True,
                "profile": _profile_view(services, result.profile),
                "probe": result.probe.to_dict(),
            }
        )
        return 0

    if args.profile_command == "list":
        values = [_profile_view(services, item) for item in services.profiles.list()]
        if args.json:
            _print_json({"profiles": values})
        elif not values:
            print("No caller profiles configured.")
        else:
            for value in values:
                print(
                    f"{value['name']}\t{value['auth_type']}\t"
                    f"{value['credential']}\t{value['endpoint']}"
                )
        return 0

    if args.profile_command == "status":
        profile = services.profiles.require(args.name)
        value = _profile_view(services, profile)
        if args.probe:
            value["probe"] = (
                await services.profile_service.probe_profile(profile.name)
            ).to_dict()
        _print_json(value)
        return 0

    if args.profile_command == "credential" and args.credential_command == "replace":
        bearer = _credential_from_input(
            stdin=args.credential_stdin,
            prompt="New delegated caller bearer: ",
        )
        profile, result = await services.profile_service.replace_credential(
            name=args.name,
            bearer=bearer,
        )
        _print_json(
            {
                "profile": _profile_view(services, profile),
                "probe": result.to_dict(),
                "client_restart_required": True,
            }
        )
        return 0

    if args.profile_command == "remove":
        removed = services.profile_service.remove(
            args.name,
            force=args.force,
            server_card_revoked=args.server_card_revoked,
            access_id=args.access_id,
        )
        _print_json(
            {
                "removed": removed.profile.name,
                "dangling_client_entries": removed.dangling_installations,
                "running_helper_stopped": False,
                "server_card_revoked": False,
                "server_card_revocation_asserted_by_operator": bool(
                    args.server_card_revoked
                ),
            }
        )
        return 0

    if args.profile_command == "disconnect":
        removed = await services.profile_service.disconnect(
            args.name,
            force=args.force,
        )
        _print_json(
            {
                "disconnected": removed.profile.name,
                "access_id": removed.profile.access_id,
                "dangling_client_entries": removed.dangling_installations,
                "running_helper_stopped": False,
                "server_card_revoked": True,
                "local_credential_removed": True,
            }
        )
        return 0

    raise AssertionError("unhandled profile command")


def _run_client(args: argparse.Namespace, services: Services) -> int:
    if args.client_command == "list":
        installations = [item.to_dict() for item in services.installations.list()]
        if args.json:
            _print_json({"installations": installations})
        elif not installations:
            print("No managed client entries configured.")
        else:
            for item in installations:
                target = item["profile"] or item["endpoint"]
                print(
                    f"{item['client']}\t{item['server_name']}\t{item['mode']}\t{target}"
                )
        return 0

    if args.client_command == "install":
        result = services.client_service.install(
            client=args.client,
            profile_name=args.profile,
            endpoint=args.endpoint,
            mode=args.mode,
            server_name=args.name,
        )
        _print_json(
            {
                "changed": result.changed,
                "installation": result.installation.to_dict(),
                "requested_mode": result.requested_mode,
                "selected_mode": result.installation.mode,
                "selection_reason": result.selection_reason,
                "authorization_required": result.authorization_command is not None,
                "authorization_command": (
                    list(result.authorization_command)
                    if result.authorization_command is not None
                    else None
                ),
                "client_reload_may_be_required": result.changed,
            }
        )
        return 0

    if args.client_command == "command":
        _print_json(services.client_service.helper_entry(profile_name=args.profile))
        return 0

    if args.client_command == "remove":
        result = services.client_service.remove(
            client=args.client, server_name=args.name
        )
        _print_json(
            {
                "changed": result.changed,
                "removed": result.installation.to_dict(),
                "running_helper_stopped": False,
                "server_card_revoked": False,
            }
        )
        return 0

    raise AssertionError("unhandled client command")


def _run_setup(args: argparse.Namespace, services: Services) -> int:
    auth_options_supplied = bool(
        args.auth or args.google_client_id or args.bootstrap_admin_email is not None
    )
    if args.endpoint:
        if (
            args.release
            or args.upstream
            or args.build
            or args.runtime_root
            or args.repository
            or auth_options_supplied
        ):
            raise ConnectionHubCliError(
                "source_selector_not_applicable",
                "Platform source, authentication, and runtime-root options apply only when creating a local host.",
            )
        if not args.tenant or not args.project:
            raise ConnectionHubCliError(
                "endpoint_coordinates_required",
                "Endpoint setup requires both --tenant and --project.",
            )
        result = services.host_service.setup_endpoint(
            endpoint=args.endpoint,
            tenant=args.tenant,
            project=args.project,
            replace=args.replace,
            open_browser=not args.no_open,
        )
    elif args.local_workdir:
        if args.tenant or args.project:
            raise ConnectionHubCliError(
                "existing_local_coordinates_not_applicable",
                "An existing local runtime supplies its tenant and project coordinates.",
            )
        if args.release or args.upstream or args.build:
            raise ConnectionHubCliError(
                "source_selector_not_applicable",
                "Release, upstream, and build options apply only when creating a local host.",
            )
        if auth_options_supplied:
            raise ConnectionHubCliError(
                "auth_selector_not_applicable",
                "An existing local runtime keeps the authentication declared in its staged descriptors.",
            )
        result = services.host_service.setup_existing_local(
            workdir=Path(args.local_workdir),
            replace=args.replace,
            start=not args.no_start,
            open_browser=not args.no_open,
            timeout_seconds=args.wait_seconds,
        )
    else:
        auth_type, google_client_id, bootstrap_admin_email = _new_local_auth(args)
        result = services.host_service.setup_new_local(
            runtime_root=Path(args.runtime_root or DEFAULT_RUNTIME_ROOT),
            tenant=args.tenant or "local",
            project=args.project or "connection-hub",
            repository=args.repository or LocalPlatformSourceRequest().repository,
            release_ref=args.release,
            upstream=args.upstream,
            build=args.build,
            auth_type=auth_type,
            google_client_id=google_client_id,
            bootstrap_admin_email=bootstrap_admin_email,
            replace=args.replace,
            start=not args.no_start,
            open_browser=not args.no_open,
            timeout_seconds=args.wait_seconds,
        )
    _print_json(result)
    return 0


def _management_view(
    request: ManagementRequest,
    result: ManagementResult | ManagementDenial,
) -> dict[str, Any]:
    if isinstance(result, ManagementResult):
        return {
            "schema": "connection_hub_cli.management_result.v1",
            "ok": True,
            "operation": result.operation,
            "resource": result.resource,
            "invocation": {
                "id": result.invocation_id,
                "replay": result.replay,
            },
            "authority": dict(result.authority),
            "result": dict(result.result),
        }
    value: dict[str, Any] = {
        "schema": "connection_hub_cli.management_error.v1",
        "ok": False,
        "status": result.status,
        "operation": request.operation,
        "resource": request.target.resource,
        "invocation_id": request.invocation_id,
        "request_digest": request.request_digest,
        "error": {"code": result.code, "retryable": result.retryable},
    }
    if result.recovery is not None:
        recovery = result.recovery
        value["recovery"] = {
            "type": "consent_required",
            "reason": "delegated_request_permit_required",
            "authorization_url": recovery.authorization_url,
            "access_id": recovery.access_id,
            "resource": recovery.resource,
            "operation": recovery.operation,
            "application_id": recovery.application_id,
            "invocation_id": recovery.invocation_id,
            "request_digest": recovery.request_digest,
            "card_revision": recovery.card_revision,
            "catalog_version": recovery.catalog_version,
            "expires_at": recovery.expires_at,
            "choices": list(recovery.choices),
        }
    return value


async def _execute_management(
    args: argparse.Namespace,
    services: Services,
    request: ManagementRequest,
) -> int:
    result = await services.management_service.execute(request)
    if isinstance(result, ManagementDenial) and result.recovery is not None:
        recovery = result.recovery
        opened = False
        if recovery.expires_at > int(time.time()) and not args.no_open:
            try:
                opened = bool(webbrowser.open(recovery.authorization_url))
            except Exception:  # noqa: BLE001
                opened = False
        if opened and sys.stdin.isatty() and not args.no_wait:
            input("Approve the exact operation in the browser, then press Enter: ")
            result = await services.management_service.execute(request)
    _print_json(_management_view(request, result))
    return 0 if isinstance(result, ManagementResult) else 3


async def _run_host(args: argparse.Namespace, services: Services) -> int:
    if args.host_command == "status":
        _print_json(services.host_service.status(probe=args.probe))
        return 0
    if args.host_command == "start":
        _print_json(
            services.host_service.start(
                build=args.build, timeout_seconds=args.wait_seconds
            )
        )
        return 0
    if args.host_command == "stop":
        _print_json(services.host_service.stop(remove_volumes=args.remove_volumes))
        return 0
    if args.host_command == "open":
        _print_json(services.host_service.open())
        return 0
    if args.host_command == "authorize":
        target = services.host_service.management_target()
        authorization_options: dict[str, Any] = {}
        if args.no_open:
            authorization_options["browser_opener"] = _print_manual_authorization_url
        result = await services.authorization_flow.authorize_and_store(
            target_key=target.session_target_key,
            protected_resource_metadata_url=(target.protected_resource_metadata_url),
            resource=target.resource,
            scope=args.scope,
            provisioned_client_id=args.client_id,
            timeout_seconds=args.wait_seconds,
            **authorization_options,
        )
        output = {
            "authorized": True,
            "target": {
                "tenant": target.tenant,
                "project": target.project,
                "resource": target.resource,
            },
            "credential": "stored",
        }
        if result.session.access_id:
            output["access_id"] = result.session.access_id
        _print_json(output)
        return 0
    if args.host_command == "disconnect":
        target = services.host_service.management_target()
        removed = await services.management_service.disconnect(
            target.session_target_key
        )
        output = {
            "disconnected": True,
            "target": {
                "tenant": target.tenant,
                "project": target.project,
                "resource": target.resource,
            },
            "server_card_revoked": True,
            "local_credential_removed": True,
        }
        if removed.access_id:
            output["access_id"] = removed.access_id
        _print_json(output)
        return 0
    if args.host_command == "inspect":
        target = services.host_service.management_target()
        return await _execute_management(
            args,
            services,
            ManagementRequest.inspect(
                target,
                invocation_id=args.invocation_id,
            ),
        )
    if args.host_command == "surfaces":
        target = services.host_service.management_target()
        return await _execute_management(
            args,
            services,
            ManagementRequest.surfaces(
                target,
                application_id=args.application_id,
                invocation_id=args.invocation_id,
            ),
        )
    if args.host_command == "reload":
        target = services.host_service.management_target()
        return await _execute_management(
            args,
            services,
            ManagementRequest.reload(
                target,
                application_id=args.application_id,
                invocation_id=args.invocation_id,
            ),
        )
    raise AssertionError("unhandled host command")


async def _run(args: argparse.Namespace) -> int:
    services = build_services()
    if args.command == "setup":
        return _run_setup(args, services)
    if args.command == "host":
        return await _run_host(args, services)
    if args.command == "open":
        _print_json(services.host_service.open())
        return 0
    if args.command == "profile":
        return await _run_profile(args, services)
    if args.command == "client":
        return _run_client(args, services)
    if args.command == "mcp":
        if args.installation_id:
            known = any(
                item.installation_id == args.installation_id
                and item.profile == args.profile
                for item in services.installations.list()
            )
            if not known:
                raise ConnectionHubCliError(
                    "installation_not_found",
                    "This managed MCP client installation is no longer registered locally.",
                )
        await serve_profile(
            profile_name=args.profile,
            profiles=services.profiles,
            credentials=services.credentials,
            oauth_sessions=services.oauth_profile_sessions,
        )
        return 0
    if args.command == "status":
        _print_json(
            {
                "version": __version__,
                "credential_store": services.credentials.backend_name(),
                "profiles": [
                    _profile_view(services, item) for item in services.profiles.list()
                ],
                "installations": [
                    item.to_dict() for item in services.installations.list()
                ],
                "host": services.host_service.status(probe=False),
                "management_sessions": [
                    {
                        "target_key": item.target_key,
                        "resource": item.resource,
                        "access_id": item.access_id,
                        "credential": (
                            "present"
                            if services.oauth_repository.credential_present(
                                item.session_id
                            )
                            else "missing"
                        ),
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                    }
                    for item in services.oauth_sessions.list()
                ],
            }
        )
        return 0
    if args.command == "doctor":
        diagnostics = await collect_diagnostics(
            profiles=services.profiles,
            installations=services.installations,
            credentials=services.credentials,
            profile_service=services.profile_service,
            adapters=services.adapters,
            probe=args.probe,
            oauth_sessions=services.oauth_sessions,
            oauth_repository=services.oauth_repository,
            oauth_profile_sessions=services.oauth_profile_sessions,
        )
        payload = [item.to_dict() for item in diagnostics]
        payload.extend(services.host_service.diagnostics(probe=args.probe))
        if args.json:
            _print_json({"diagnostics": payload})
        else:
            for item in payload:
                print(
                    f"{str(item['severity']).upper()}\t{item['code']}\t{item['summary']}"
                )
                if item.get("recovery"):
                    print(f"  {item['recovery']}")
        return 1 if any(item["severity"] == "error" for item in payload) else 0
    raise AssertionError("unhandled command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="connection-hub")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup", help="Create or select the KDCube host for Connection Hub."
    )
    setup_target = setup.add_mutually_exclusive_group()
    setup_target.add_argument(
        "--create-local",
        action="store_true",
        help="Create the dedicated local host (the default).",
    )
    setup_target.add_argument(
        "--local-workdir", help="Select an existing initialized KDCube workdir."
    )
    setup_target.add_argument(
        "--endpoint",
        help="Select an existing KDCube endpoint by loopback, IP, or DNS address.",
    )
    setup.add_argument("--tenant")
    setup.add_argument("--project")
    setup.add_argument(
        "--auth",
        choices=("google", "simple"),
        help="Login for a new local host (default: google; simple is for development).",
    )
    setup.add_argument(
        "--google-client-id",
        help="Google OAuth Web application client ID for the default login.",
    )
    setup.add_argument(
        "--bootstrap-admin-email",
        help="Verified Google email granted bootstrap platform administration.",
    )
    setup.add_argument(
        "--runtime-root",
        help="Parent directory for a newly created namespaced local runtime.",
    )
    setup.add_argument(
        "--repository",
        help=(
            "KDCube platform repository used for a new local runtime "
            "(default: https://github.com/kdcube/kdcube.git)."
        ),
    )
    setup_source = setup.add_mutually_exclusive_group()
    setup_source.add_argument("--release", help="Use one KDCube release ref.")
    setup_source.add_argument(
        "--upstream", action="store_true", help="Build the current upstream source."
    )
    setup.add_argument(
        "--build", action="store_true", help="Build local platform images from source."
    )
    setup.add_argument(
        "--no-start", action="store_true", help="Prepare without starting a local host."
    )
    setup.add_argument(
        "--no-open", action="store_true", help="Do not open the browser application."
    )
    setup.add_argument(
        "--replace", action="store_true", help="Replace another selected host."
    )
    setup.add_argument("--wait-seconds", type=float, default=180.0)

    commands.add_parser("open", help="Open the selected Connection Hub application.")

    host = commands.add_parser("host", help="Operate the selected application host.")
    host_commands = host.add_subparsers(dest="host_command", required=True)
    host_status = host_commands.add_parser("status", help="Inspect the selected host.")
    host_status.add_argument("--probe", action="store_true")
    host_start = host_commands.add_parser(
        "start", help="Start the selected local host."
    )
    host_start.add_argument("--build", action="store_true")
    host_start.add_argument("--wait-seconds", type=float, default=180.0)
    host_stop = host_commands.add_parser("stop", help="Stop the selected local host.")
    host_stop.add_argument("--remove-volumes", action="store_true")
    host_commands.add_parser("open", help="Open the selected application.")
    host_authorize = host_commands.add_parser(
        "authorize",
        help="Authorize this CLI through the selected KDCube login.",
    )
    host_authorize.add_argument(
        "--client-id",
        help="Provisioned public OAuth client ID when registration is unavailable.",
    )
    host_authorize.add_argument(
        "--scope",
        default=DEFAULT_MANAGEMENT_SCOPE,
        help=(
            "OAuth scopes requested from the selected KDCube deployment "
            "(default: inspect deployment and read application surfaces)."
        ),
    )
    host_authorize.add_argument(
        "--no-open",
        action="store_true",
        help="Print the authorization URL and wait for a browser callback.",
    )
    host_authorize.add_argument("--wait-seconds", type=float, default=300.0)
    host_commands.add_parser(
        "disconnect",
        help="Revoke this CLI's delegated card and remove its local OAuth session.",
    )

    def add_management_options(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--invocation-id",
            help="Reuse this exact idempotency key after approving a denied request.",
        )
        command.add_argument(
            "--no-open",
            action="store_true",
            help="Return consent recovery without opening its browser page.",
        )
        command.add_argument(
            "--no-wait",
            action="store_true",
            help="Open consent without waiting for an interactive retry.",
        )

    host_inspect = host_commands.add_parser(
        "inspect",
        help="Inspect the running deployment through delegated authority.",
    )
    add_management_options(host_inspect)
    host_surfaces = host_commands.add_parser(
        "surfaces",
        help="Read one application's declared public surfaces.",
    )
    host_surfaces.add_argument("application_id")
    add_management_options(host_surfaces)
    host_reload = host_commands.add_parser(
        "reload",
        help="Reload one exact application through delegated authority.",
    )
    host_reload.add_argument("application_id")
    add_management_options(host_reload)

    commands.add_parser(
        "status", help="Show non-secret local Connection Hub client state."
    )
    doctor = commands.add_parser(
        "doctor", help="Check local profiles, credentials, and client entries."
    )
    doctor.add_argument(
        "--probe", action="store_true", help="Connect to every configured MCP endpoint."
    )
    doctor.add_argument(
        "--json", action="store_true", help="Write structured diagnostics."
    )

    profile = commands.add_parser("profile", help="Manage delegated caller profiles.")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_add = profile_commands.add_parser(
        "add", help="Validate and store a delegated caller profile."
    )
    profile_add.add_argument("name")
    profile_add.add_argument(
        "--endpoint",
        help="Governed MCP endpoint; defaults to the selected application host.",
    )
    profile_add.add_argument("--access-id")
    profile_add.add_argument("--credential-stdin", action="store_true")
    profile_authorize = profile_commands.add_parser(
        "authorize",
        help="Create an OAuth-backed profile through the MCP endpoint's login.",
    )
    profile_authorize.add_argument("name")
    profile_authorize.add_argument(
        "--endpoint",
        help="Governed MCP endpoint; defaults to the selected application host.",
    )
    profile_authorize.add_argument(
        "--scope",
        default="",
        help="OAuth scopes; defaults to the MCP endpoint's advertised scope.",
    )
    client_registration = profile_authorize.add_mutually_exclusive_group()
    client_registration.add_argument(
        "--client-id",
        help="Provisioned public OAuth client ID when registration is unavailable.",
    )
    client_registration.add_argument(
        "--client-metadata-url",
        help="HTTPS OAuth Client ID Metadata Document URL for CIMD-capable servers.",
    )
    profile_authorize.add_argument(
        "--callback-port",
        type=int,
        help="Fixed loopback port published by the selected Client ID Metadata Document.",
    )
    profile_authorize.add_argument(
        "--no-open",
        action="store_true",
        help="Print the authorization URL and wait for a browser callback.",
    )
    profile_authorize.add_argument("--wait-seconds", type=float, default=300.0)
    profile_list = profile_commands.add_parser(
        "list", help="List caller profiles without credentials."
    )
    profile_list.add_argument("--json", action="store_true")
    profile_status = profile_commands.add_parser(
        "status", help="Inspect one caller profile."
    )
    profile_status.add_argument("name")
    profile_status.add_argument("--probe", action="store_true")
    profile_remove = profile_commands.add_parser(
        "remove", help="Remove local profile state and native credential custody."
    )
    profile_remove.add_argument("name")
    profile_remove.add_argument("--force", action="store_true")
    profile_remove.add_argument(
        "--server-card-revoked",
        action="store_true",
        help="Confirm that the recorded OAuth caller card was already revoked.",
    )
    profile_remove.add_argument(
        "--access-id",
        help="Exact recorded access_id required with --server-card-revoked.",
    )
    profile_disconnect = profile_commands.add_parser(
        "disconnect",
        help="Revoke an OAuth profile's caller card, then remove local custody.",
    )
    profile_disconnect.add_argument("name")
    profile_disconnect.add_argument("--force", action="store_true")
    profile_credential = profile_commands.add_parser(
        "credential", help="Manage a profile's local credential."
    )
    credential_commands = profile_credential.add_subparsers(
        dest="credential_command", required=True
    )
    credential_replace = credential_commands.add_parser(
        "replace",
        help="Validate a candidate before replacing the native-store credential.",
    )
    credential_replace.add_argument("name")
    credential_replace.add_argument("--credential-stdin", action="store_true")

    client = commands.add_parser(
        "client", help="Install the local helper into MCP clients."
    )
    client_commands = client.add_subparsers(dest="client_command", required=True)
    client_list = client_commands.add_parser(
        "list", help="List entries managed by Connection Hub."
    )
    client_list.add_argument("--json", action="store_true")
    client_install = client_commands.add_parser(
        "install", help="Install a native OAuth or local bridge MCP entry."
    )
    client_install.add_argument("client", choices=SUPPORTED_CLIENTS)
    client_install.add_argument(
        "--mode",
        choices=("auto", "oauth", "bridge"),
        default="auto",
        help="Prefer native OAuth, require native OAuth, or use local OS-store custody.",
    )
    client_install.add_argument(
        "--profile",
        help="Local caller profile used by bridge mode or as auto fallback.",
    )
    client_install.add_argument(
        "--endpoint",
        help="Governed MCP URL used by native OAuth mode.",
    )
    client_install.add_argument("--name", help="Override the MCP server entry name.")
    client_command = client_commands.add_parser(
        "command",
        help="Print the non-secret stdio command for another MCP client.",
    )
    client_command.add_argument("--profile", required=True)
    client_remove = client_commands.add_parser(
        "remove", help="Remove one managed MCP client entry."
    )
    client_remove.add_argument("client", choices=SUPPORTED_CLIENTS)
    client_remove.add_argument("name", help="The managed MCP server entry name.")

    mcp = commands.add_parser("mcp", help="Run the local MCP helper.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser(
        "serve", help="Relay stdio MCP to one governed caller profile."
    )
    mcp_serve.add_argument("--profile", required=True)
    mcp_serve.add_argument("--installation-id", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return anyio.run(_run, args)
    except ConnectionHubCliError as exc:
        sys.stderr.write(f"error[{exc.code}]: {exc.message}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        sys.stderr.write(
            "error[internal_error]: Connection Hub could not complete the command.\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

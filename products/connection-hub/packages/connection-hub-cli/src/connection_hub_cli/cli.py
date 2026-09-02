from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import anyio
from kdcube_cli.control import DEFAULT_RUNTIME_ROOT, LocalPlatformSourceRequest

from connection_hub_cli import __version__
from connection_hub_cli.clients import ClientService, build_client_adapters
from connection_hub_cli.credentials import MacOSKeychainCredentialStore
from connection_hub_cli.diagnostics import collect_diagnostics
from connection_hub_cli.errors import ConnectionHubCliError
from connection_hub_cli.host import HostService
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
    credentials: MacOSKeychainCredentialStore
    profile_service: ProfileService
    client_service: ClientService
    host_service: HostService
    adapters: dict[str, Any]


def build_services(*, paths: StatePaths | None = None) -> Services:
    selected_paths = paths or StatePaths.default()
    profiles = ProfileStore(selected_paths.profiles)
    installations = InstallationStore(selected_paths.installations)
    hosts = HostStore(selected_paths.host)
    credentials = MacOSKeychainCredentialStore()
    adapters = build_client_adapters()
    profile_service = ProfileService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        probe=probe_remote_tools,
    )
    client_service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters=adapters,
        launch=resolve_helper_launch(),
    )
    host_service = HostService(store=hosts)
    return Services(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=profile_service,
        client_service=client_service,
        host_service=host_service,
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


def _profile_view(services: Services, profile: Any) -> dict[str, Any]:
    return {
        "name": profile.name,
        "endpoint": profile.endpoint,
        "access_id": profile.access_id,
        "credential": "present"
        if services.profile_service.credential_present(profile)
        else "missing",
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


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

    if args.profile_command == "list":
        values = [_profile_view(services, item) for item in services.profiles.list()]
        if args.json:
            _print_json({"profiles": values})
        elif not values:
            print("No caller profiles configured.")
        else:
            for value in values:
                print(f"{value['name']}\t{value['credential']}\t{value['endpoint']}")
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
        removed = services.profile_service.remove(args.name, force=args.force)
        _print_json(
            {
                "removed": removed.profile.name,
                "dangling_client_entries": removed.dangling_installations,
                "running_helper_stopped": False,
                "server_card_revoked": False,
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
                print(f"{item['client']}\t{item['server_name']}\t{item['profile']}")
        return 0

    if args.client_command == "install":
        result = services.client_service.install(
            client=args.client,
            profile_name=args.profile,
            server_name=args.name,
        )
        _print_json(
            {
                "changed": result.changed,
                "installation": result.installation.to_dict(),
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


def _run_host(args: argparse.Namespace, services: Services) -> int:
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
    raise AssertionError("unhandled host command")


async def _run(args: argparse.Namespace) -> int:
    services = build_services()
    if args.command == "setup":
        return _run_setup(args, services)
    if args.command == "host":
        return _run_host(args, services)
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
        "remove", help="Remove local profile state and Keychain custody."
    )
    profile_remove.add_argument("name")
    profile_remove.add_argument("--force", action="store_true")
    profile_credential = profile_commands.add_parser(
        "credential", help="Manage a profile's local credential."
    )
    credential_commands = profile_credential.add_subparsers(
        dest="credential_command", required=True
    )
    credential_replace = credential_commands.add_parser(
        "replace",
        help="Validate a candidate before replacing the working Keychain credential.",
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
        "install", help="Install one profile into one MCP client."
    )
    client_install.add_argument("client", choices=SUPPORTED_CLIENTS)
    client_install.add_argument("--profile", required=True)
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
    except Exception:
        sys.stderr.write(
            "error[internal_error]: Connection Hub could not complete the command.\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from connection_hub_cli.errors import ClientConfigurationError, ProfileError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CLIENTS = frozenset({"claude-code", "claude-desktop", "codex", "hermes", "openclaw"})
_HOST_KINDS = frozenset({"local", "endpoint"})
_PROFILE_AUTH_TYPES = frozenset({"static_bearer", "oauth"})
_OAUTH_CLIENT_SOURCES = frozenset({"cimd", "dcr", "provisioned"})
_INSTALLATION_MODES = frozenset({"bridge", "oauth"})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_name(value: str, *, field: str = "profile name") -> str:
    candidate = str(value or "").strip()
    if not _NAME_RE.fullmatch(candidate):
        raise ProfileError(
            "invalid_name",
            f"{field} must start with a letter or digit and contain at most 64 letters, digits, '.', '_', or '-'.",
        )
    return candidate


def _is_loopback_host(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def endpoint_address_kind(endpoint: str) -> str:
    hostname = urlsplit(endpoint).hostname or ""
    if _is_loopback_host(hostname):
        return "loopback"
    try:
        ipaddress.ip_address(hostname.rstrip("."))
    except ValueError:
        return "dns"
    return "ip"


def validate_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError as exc:
        raise ProfileError(
            "invalid_endpoint", "The MCP endpoint URL is invalid."
        ) from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProfileError(
            "invalid_endpoint",
            "The MCP endpoint must be an absolute HTTP or HTTPS URL.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ProfileError(
            "endpoint_contains_credentials",
            "The MCP endpoint must not contain user information or credentials.",
        )
    if parsed.query or parsed.fragment:
        raise ProfileError(
            "endpoint_contains_query",
            "The MCP endpoint must not contain a query string or fragment.",
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ProfileError(
            "insecure_endpoint",
            "Plain HTTP is accepted only for a loopback Connection Hub endpoint.",
        )
    return endpoint


def validate_access_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if (
        not candidate
        or len(candidate) > 256
        or any(ch.isspace() or ord(ch) < 32 for ch in candidate)
    ):
        raise ProfileError(
            "invalid_access_id",
            "The access_id must be a non-empty value without whitespace or control characters.",
        )
    return candidate


def _profile_oauth_text(value: Any, *, field: str, maximum: int = 8192) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
    ):
        raise ProfileError(
            "invalid_oauth_profile_record",
            f"The OAuth profile {field} is invalid.",
        )
    return candidate


def _profile_oauth_url(value: Any, *, field: str) -> str:
    candidate = _profile_oauth_text(value, field=field)
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        raise ProfileError(
            "invalid_oauth_profile_record",
            f"The OAuth profile {field} is invalid.",
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme == "http" and not _is_loopback_host(parsed.hostname))
    ):
        raise ProfileError(
            "invalid_oauth_profile_record",
            f"The OAuth profile {field} is invalid.",
        )
    return candidate


@dataclass(frozen=True, slots=True)
class ProfileOAuthMetadata:
    protected_resource_metadata_url: str
    resource: str
    issuer: str
    token_endpoint: str
    revocation_endpoint: str | None
    client_id: str
    client_source: str
    client_metadata_url: str | None
    scope: str

    def verify(self) -> None:
        _profile_oauth_url(
            self.protected_resource_metadata_url,
            field="protected-resource metadata URL",
        )
        _profile_oauth_text(self.resource, field="resource")
        _profile_oauth_url(self.issuer, field="issuer")
        _profile_oauth_url(self.token_endpoint, field="token endpoint")
        if self.revocation_endpoint is not None:
            _profile_oauth_url(
                self.revocation_endpoint,
                field="revocation endpoint",
            )
        _profile_oauth_text(self.client_id, field="client identifier", maximum=4096)
        if self.client_source not in _OAUTH_CLIENT_SOURCES:
            raise ProfileError(
                "invalid_oauth_profile_record",
                "The OAuth profile client registration source is invalid.",
            )
        if self.client_metadata_url is not None:
            _profile_oauth_url(
                self.client_metadata_url,
                field="client metadata URL",
            )
        if len(self.scope) > 8192 or any(
            not 0x20 <= ord(ch) <= 0x7E for ch in self.scope
        ):
            raise ProfileError(
                "invalid_oauth_profile_record",
                "The OAuth profile scope is invalid.",
            )

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "protected_resource_metadata_url": self.protected_resource_metadata_url,
            "resource": self.resource,
            "issuer": self.issuer,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "client_id": self.client_id,
            "client_source": self.client_source,
            "client_metadata_url": self.client_metadata_url,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProfileOAuthMetadata:
        try:
            record = cls(
                protected_resource_metadata_url=str(
                    value["protected_resource_metadata_url"]
                ),
                resource=str(value["resource"]),
                issuer=str(value["issuer"]),
                token_endpoint=str(value["token_endpoint"]),
                revocation_endpoint=(
                    str(value["revocation_endpoint"])
                    if value.get("revocation_endpoint")
                    else None
                ),
                client_id=str(value["client_id"]),
                client_source=str(value["client_source"]),
                client_metadata_url=(
                    str(value["client_metadata_url"])
                    if value.get("client_metadata_url")
                    else None
                ),
                scope=str(value.get("scope") or ""),
            )
            record.verify()
            return record
        except (KeyError, TypeError, ValueError, ProfileError) as exc:
            raise ProfileError(
                "invalid_oauth_profile_record",
                "The stored OAuth profile metadata is invalid.",
            ) from exc


@dataclass(frozen=True, slots=True)
class CallerProfile:
    name: str
    endpoint: str
    credential_ref: str
    access_id: str | None
    auth_type: str
    oauth: ProfileOAuthMetadata | None
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        endpoint: str,
        access_id: str | None = None,
        credential_ref: str | None = None,
        now: str | None = None,
    ) -> CallerProfile:
        timestamp = now or utc_now()
        return cls(
            name=validate_name(name),
            endpoint=validate_endpoint(endpoint),
            credential_ref=credential_ref or uuid.uuid4().hex,
            access_id=validate_access_id(access_id),
            auth_type="static_bearer",
            oauth=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def create_oauth(
        cls,
        *,
        name: str,
        endpoint: str,
        access_id: str,
        oauth: ProfileOAuthMetadata,
        credential_ref: str | None = None,
        now: str | None = None,
    ) -> CallerProfile:
        oauth.verify()
        timestamp = now or utc_now()
        return cls(
            name=validate_name(name),
            endpoint=validate_endpoint(endpoint),
            credential_ref=credential_ref or uuid.uuid4().hex,
            access_id=validate_access_id(access_id),
            auth_type="oauth",
            oauth=oauth,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def with_credential_replaced(self, *, now: str | None = None) -> CallerProfile:
        return CallerProfile(
            name=self.name,
            endpoint=self.endpoint,
            credential_ref=self.credential_ref,
            access_id=self.access_id,
            auth_type=self.auth_type,
            oauth=self.oauth,
            created_at=self.created_at,
            updated_at=now or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "credential_ref": self.credential_ref,
            "access_id": self.access_id,
            "auth_type": self.auth_type,
            "oauth": self.oauth.to_dict() if self.oauth is not None else None,
            "record_version": 2,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CallerProfile:
        try:
            profile = cls(
                name=validate_name(str(value["name"])),
                endpoint=validate_endpoint(str(value["endpoint"])),
                credential_ref=str(value["credential_ref"]),
                access_id=validate_access_id(value.get("access_id")),
                auth_type=str(value.get("auth_type") or "static_bearer"),
                oauth=(
                    ProfileOAuthMetadata.from_dict(value["oauth"])
                    if isinstance(value.get("oauth"), dict)
                    else None
                ),
                created_at=str(value["created_at"]),
                updated_at=str(value["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError(
                "invalid_profile_record", "A stored caller profile is invalid."
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{32}", profile.credential_ref):
            raise ProfileError(
                "invalid_profile_record",
                "A stored caller profile has an invalid credential reference.",
            )
        if (
            profile.auth_type not in _PROFILE_AUTH_TYPES
            or (
                profile.auth_type == "oauth"
                and (profile.oauth is None or profile.access_id is None)
            )
            or (profile.auth_type == "static_bearer" and profile.oauth is not None)
        ):
            raise ProfileError(
                "invalid_profile_record",
                "A stored caller profile has an invalid authorization type.",
            )
        return profile


@dataclass(frozen=True, slots=True)
class ProbeResult:
    tool_count: int
    server_name: str | None = None
    server_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_count": self.tool_count,
            "server_name": self.server_name,
            "server_version": self.server_version,
        }


@dataclass(frozen=True, slots=True)
class HostSelection:
    kind: str
    tenant: str
    project: str
    application_id: str
    widget_alias: str
    mcp_alias: str
    workdir: str | None
    endpoint: str | None
    created_at: str
    updated_at: str

    @classmethod
    def local(
        cls,
        *,
        workdir: str,
        tenant: str,
        project: str,
        now: str | None = None,
    ) -> HostSelection:
        timestamp = now or utc_now()
        return cls(
            kind="local",
            tenant=validate_name(tenant, field="tenant"),
            project=validate_name(project, field="project"),
            application_id="connection-hub@1-0",
            widget_alias="connections_settings",
            mcp_alias="remote_mcp_proxy",
            workdir=str(Path(workdir).expanduser().resolve()),
            endpoint=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def endpoint_target(
        cls,
        *,
        endpoint: str,
        tenant: str,
        project: str,
        now: str | None = None,
    ) -> HostSelection:
        timestamp = now or utc_now()
        return cls(
            kind="endpoint",
            tenant=validate_name(tenant, field="tenant"),
            project=validate_name(project, field="project"),
            application_id="connection-hub@1-0",
            widget_alias="connections_settings",
            mcp_alias="remote_mcp_proxy",
            workdir=None,
            endpoint=validate_endpoint(endpoint).rstrip("/"),
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def target_key(self) -> str:
        coordinate = self.workdir if self.kind == "local" else self.endpoint
        return f"{self.kind}:{coordinate}:{self.tenant}:{self.project}"

    def refreshed(self, *, now: str | None = None) -> HostSelection:
        return HostSelection(
            kind=self.kind,
            tenant=self.tenant,
            project=self.project,
            application_id=self.application_id,
            widget_alias=self.widget_alias,
            mcp_alias=self.mcp_alias,
            workdir=self.workdir,
            endpoint=self.endpoint,
            created_at=self.created_at,
            updated_at=now or utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "kind": self.kind,
            "tenant": self.tenant,
            "project": self.project,
            "application_id": self.application_id,
            "widget_alias": self.widget_alias,
            "mcp_alias": self.mcp_alias,
            "workdir": self.workdir,
            "endpoint": self.endpoint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.endpoint:
            value["address"] = {
                "host": urlsplit(self.endpoint).hostname,
                "kind": endpoint_address_kind(self.endpoint),
            }
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HostSelection:
        try:
            kind = str(value["kind"])
            tenant = validate_name(str(value["tenant"]), field="tenant")
            project = validate_name(str(value["project"]), field="project")
            application_id = str(value["application_id"])
            widget_alias = str(value["widget_alias"])
            mcp_alias = str(value["mcp_alias"])
            workdir = value.get("workdir")
            endpoint = value.get("endpoint")
            created_at = str(value["created_at"])
            updated_at = str(value["updated_at"])
        except (KeyError, TypeError, ValueError, ProfileError) as exc:
            raise ProfileError(
                "invalid_host_record", "The stored application host record is invalid."
            ) from exc
        if (
            kind not in _HOST_KINDS
            or application_id != "connection-hub@1-0"
            or not widget_alias
            or not mcp_alias
        ):
            raise ProfileError(
                "invalid_host_record", "The stored application host record is invalid."
            )
        if kind == "local":
            if not isinstance(workdir, str) or endpoint is not None:
                raise ProfileError(
                    "invalid_host_record",
                    "The stored application host record is invalid.",
                )
            workdir = str(Path(workdir).expanduser().resolve())
        else:
            if not isinstance(endpoint, str) or workdir is not None:
                raise ProfileError(
                    "invalid_host_record",
                    "The stored application host record is invalid.",
                )
            endpoint = validate_endpoint(endpoint).rstrip("/")
        return cls(
            kind=kind,
            tenant=tenant,
            project=project,
            application_id=application_id,
            widget_alias=widget_alias,
            mcp_alias=mcp_alias,
            workdir=workdir,
            endpoint=endpoint,
            created_at=created_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True, slots=True)
class HelperLaunch:
    command: str
    prefix_args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagedInstallation:
    installation_id: str
    client: str
    mode: str
    profile: str | None
    server_name: str
    endpoint: str | None
    command: str | None
    args: tuple[str, ...]
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        client: str,
        profile: str,
        server_name: str,
        launch: HelperLaunch,
        installation_id: str | None = None,
        now: str | None = None,
    ) -> ManagedInstallation:
        """Create a bridge installation using the legacy public factory."""

        return cls.create_bridge(
            client=client,
            profile=profile,
            server_name=server_name,
            launch=launch,
            installation_id=installation_id,
            now=now,
        )

    @classmethod
    def create_bridge(
        cls,
        *,
        client: str,
        profile: str,
        server_name: str,
        launch: HelperLaunch,
        installation_id: str | None = None,
        now: str | None = None,
    ) -> ManagedInstallation:
        if client not in _CLIENTS:
            raise ClientConfigurationError(
                "unsupported_client", f"Unsupported MCP client: {client}."
            )
        validated_profile = validate_name(profile)
        validated_server_name = validate_name(server_name, field="MCP server name")
        marker = installation_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", marker):
            raise ClientConfigurationError(
                "invalid_installation_id", "The client installation marker is invalid."
            )
        args = (
            *launch.prefix_args,
            "mcp",
            "serve",
            "--profile",
            validated_profile,
            "--installation-id",
            marker,
        )
        return cls(
            installation_id=marker,
            client=client,
            mode="bridge",
            profile=validated_profile,
            server_name=validated_server_name,
            endpoint=None,
            command=launch.command,
            args=tuple(args),
            created_at=now or utc_now(),
        )

    @classmethod
    def create_oauth(
        cls,
        *,
        client: str,
        endpoint: str,
        server_name: str,
        installation_id: str | None = None,
        now: str | None = None,
    ) -> ManagedInstallation:
        if client not in _CLIENTS:
            raise ClientConfigurationError(
                "unsupported_client", f"Unsupported MCP client: {client}."
            )
        validated_server_name = validate_name(server_name, field="MCP server name")
        marker = installation_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", marker):
            raise ClientConfigurationError(
                "invalid_installation_id", "The client installation marker is invalid."
            )
        return cls(
            installation_id=marker,
            client=client,
            mode="oauth",
            profile=None,
            server_name=validated_server_name,
            endpoint=validate_endpoint(endpoint),
            command=None,
            args=(),
            created_at=now or utc_now(),
        )

    @property
    def registry_key(self) -> str:
        return f"{self.client}:{self.server_name}"

    def to_entry(self, *, include_type: bool = False) -> dict[str, Any]:
        if self.mode == "oauth":
            if self.client == "openclaw":
                return {
                    "url": self.endpoint,
                    "transport": "streamable-http",
                    "auth": "oauth",
                    "requestTimeoutMs": 60000,
                    "connectionTimeoutMs": 20000,
                }
            if self.client == "hermes":
                return {
                    "url": self.endpoint,
                    "auth": "oauth",
                    "timeout": 60,
                    "connect_timeout": 20,
                }
            return {"type": "http", "url": self.endpoint}
        entry: dict[str, Any] = {"command": self.command, "args": list(self.args)}
        if include_type:
            entry["type"] = "stdio"
        return entry

    def owns_entry(self, entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        if self.client == "codex":
            transport = entry.get("transport")
            if not isinstance(transport, dict):
                return False
            if self.mode == "bridge":
                return (
                    transport.get("type") == "stdio"
                    and transport.get("command") == self.command
                    and transport.get("args") == list(self.args)
                )
            return (
                transport.get("type") == "streamable_http"
                and transport.get("url") == self.endpoint
                and transport.get("bearer_token_env_var") is None
                and transport.get("http_headers") is None
                and transport.get("env_http_headers") is None
                and transport.get("http_headers_helper") is None
            )
        if self.mode == "oauth":
            expected = self.to_entry()
            if any(entry.get(key) != value for key, value in expected.items()):
                return False
            return entry.get("headers") in (None, {})
        return entry.get("command") == self.command and entry.get("args") == list(
            self.args
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "installation_id": self.installation_id,
            "client": self.client,
            "mode": self.mode,
            "profile": self.profile,
            "server_name": self.server_name,
            "endpoint": self.endpoint,
            "command": self.command,
            "args": list(self.args),
            "record_version": 2,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ManagedInstallation:
        try:
            installation = cls(
                installation_id=str(value["installation_id"]),
                client=str(value["client"]),
                mode=str(value.get("mode") or "bridge"),
                profile=(
                    validate_name(str(value["profile"]))
                    if value.get("profile") is not None
                    else None
                ),
                server_name=validate_name(
                    str(value["server_name"]), field="MCP server name"
                ),
                endpoint=(
                    validate_endpoint(str(value["endpoint"]))
                    if value.get("endpoint") is not None
                    else None
                ),
                command=(
                    str(value["command"]) if value.get("command") is not None else None
                ),
                args=tuple(str(item) for item in value.get("args", ())),
                created_at=str(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError, ProfileError) as exc:
            raise ClientConfigurationError(
                "invalid_installation_record",
                "A stored client installation record is invalid.",
            ) from exc
        bridge_invalid = installation.mode == "bridge" and (
            installation.profile is None
            or not installation.command
            or installation.endpoint is not None
        )
        oauth_invalid = installation.mode == "oauth" and (
            installation.profile is not None
            or installation.endpoint is None
            or installation.command is not None
            or bool(installation.args)
        )
        if (
            installation.client not in _CLIENTS
            or installation.mode not in _INSTALLATION_MODES
            or not re.fullmatch(r"[0-9a-f]{32}", installation.installation_id)
            or bridge_invalid
            or oauth_invalid
        ):
            raise ClientConfigurationError(
                "invalid_installation_record",
                "A stored client installation record is invalid.",
            )
        return installation


SUPPORTED_CLIENTS = tuple(sorted(_CLIENTS))

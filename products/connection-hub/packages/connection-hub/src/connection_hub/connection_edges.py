# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""SDK client for Connection Hub connection edges.

This is the app-to-app boundary for linking and delegating identities. A
surface app such as a Telegram Mini App should expose only its own narrow HTTP
operation to the browser, then call this client server-side. The client invokes
Connection Hub over the request-bound bundle operation bridge, preserving the
current tenant/project/user context without making the browser know Connection
Hub's route shape.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

BundleOperationCaller = Callable[..., Awaitable[Mapping[str, Any]]]


DEFAULT_CONNECTION_HUB_BUNDLE_ID = "connection-hub@1-0"


def _str(value: Any) -> str:
    return str(value or "").strip()


def _unwrap_operation_result(operation: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize direct and REST-style bundle operation results.

    The in-process bridge may return either the raw operation payload or the
    same alias-wrapped shape that public REST routes expose. SDK callers should
    not need to know which transport shape was used.
    """

    if operation in result and isinstance(result.get(operation), Mapping):
        return dict(result.get(operation) or {})
    return dict(result)


def _prop(entrypoint: Any, path: str, default: Any = None) -> Any:
    getter = getattr(entrypoint, "bundle_prop", None)
    if callable(getter):
        return getter(path, default)
    props = getattr(entrypoint, "bundle_props", None)
    if not isinstance(props, Mapping):
        return default
    current: Any = props
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return default
        current = current.get(part)
    return default if current is None else current


def connection_hub_bundle_id(entrypoint: Any, *, default: str = DEFAULT_CONNECTION_HUB_BUNDLE_ID) -> str:
    """Resolve the Connection Hub app id from app config.

    `connections.connection_hub.bundle_id` is the platform-owned location for
    callers.
    """

    value = _str(_prop(entrypoint, "connections.connection_hub.bundle_id", ""))
    if value:
        return value
    return _str(default) or DEFAULT_CONNECTION_HUB_BUNDLE_ID


def _forwarded_parts(raw: Any) -> dict[str, str]:
    """First element of an RFC 7239 ``Forwarded`` header, as key/value pairs."""
    out: dict[str, str] = {}
    for item in _str(raw).split(",", 1)[0].split(";"):
        key, _, value = item.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if key and value:
            out[key] = value
    return out


def _first_header_value(raw: Any) -> str:
    return _str(raw).split(",", 1)[0].strip()


def is_local_or_internal_host(host: Any) -> bool:
    """Whether a host names this machine or a name with no public authority."""
    name = _str(host).split(":", 1)[0].lower()
    return (
        not name
        or name == "localhost"
        or name.startswith("127.")
        or name == "::1"
        or name.endswith(".local")
        or "." not in name
    )


def public_proto(proto: Any, host: Any) -> str:
    """A public host reached over http is behind a terminator that dropped the
    provenance; a local one is genuinely http."""
    value = _str(proto).lower() or "http"
    if value == "http" and not is_local_or_internal_host(host):
        return "https"
    return value


def request_origin(request: Any) -> str:
    """Public origin for links and callbacks returned to a browser.

    Scheme provenance in descending order of authority: RFC 7239 ``Forwarded``,
    then ``X-Forwarded-Proto`` (what the deployment's proxy sets under its
    declared ``proxy.forwarded_proto.source`` policy), then the scheme the
    request actually arrived on. Never a constant: a deployment reached over
    plain http must not be told it is https, or every callback and RP origin it
    mints is unreachable.
    """

    if request is None:
        return ""
    try:
        headers = request.headers
        forwarded = _forwarded_parts(headers.get("forwarded"))
        proto = (
            forwarded.get("proto")
            or _first_header_value(headers.get("x-forwarded-proto"))
            or _str(getattr(getattr(request, "url", None), "scheme", ""))
            or "http"
        )
        host = (
            forwarded.get("host")
            or _first_header_value(headers.get("x-forwarded-host"))
            or _first_header_value(headers.get("host"))
            or _str(getattr(getattr(request, "url", None), "netloc", ""))
        )
        if host:
            return f"{public_proto(proto, host)}://{host}"
    except Exception:
        pass
    try:
        url = request.url
        return f"{url.scheme}://{url.netloc}"
    except Exception:
        return ""


class ConnectionEdgesClient:
    """Typed app-to-app client for Connection Hub edge operations."""

    def __init__(
        self,
        entrypoint: Any,
        *,
        connection_hub_bundle_id: str | None = None,
        tenant: str | None = None,
        project: str | None = None,
        operation_caller: BundleOperationCaller | None = None,
    ) -> None:
        self.entrypoint = entrypoint
        self.bundle_id = _str(connection_hub_bundle_id) or connection_hub_bundle_id_from_entrypoint(entrypoint)
        self.tenant = _str(tenant) or None
        self.project = _str(project) or None
        self._operation_caller = operation_caller

    async def telegram_edge_start(
        self,
        *,
        telegram_init_data: str = "",
        public_origin: str = "",
    ) -> dict[str, Any]:
        return await self._public_call(
            "telegram_connection_edge_start",
            {
                "telegram_init_data": _str(telegram_init_data),
                "request_origin": _str(public_origin),
            },
        )

    async def telegram_edge_complete(
        self,
        *,
        challenge_id: str,
        telegram_init_data: str = "",
        public_origin: str = "",
    ) -> dict[str, Any]:
        return await self._public_call(
            "telegram_connection_edge_complete",
            {
                "challenge_id": _str(challenge_id),
                "telegram_init_data": _str(telegram_init_data),
                "request_origin": _str(public_origin),
            },
        )

    async def telegram_edge_status(
        self,
        *,
        telegram_init_data: str = "",
        public_origin: str = "",
    ) -> dict[str, Any]:
        return await self._public_call(
            "telegram_connection_edge_status",
            {
                "telegram_init_data": _str(telegram_init_data),
                "request_origin": _str(public_origin),
            },
            http_method="GET",
        )

    async def resolve_identity(self, *, provider: str, provider_subject: str) -> dict[str, Any]:
        return await self._operation_call(
            "identity_resolve",
            {
                "provider": _str(provider),
                "provider_subject": _str(provider_subject),
            },
        )

    async def _public_call(self, operation: str, data: Mapping[str, Any], *, http_method: str = "POST") -> dict[str, Any]:
        return await self._call(operation, data=data, route="public", http_method=http_method)

    async def _operation_call(self, operation: str, data: Mapping[str, Any], *, http_method: str = "POST") -> dict[str, Any]:
        return await self._call(operation, data=data, route="operations", http_method=http_method)

    async def _call(self, operation: str, *, data: Mapping[str, Any], route: str, http_method: str) -> dict[str, Any]:
        if self._operation_caller is None:
            raise RuntimeError("ConnectionEdgesClient requires an application-operation caller")
        result = await self._operation_caller(
            bundle_id=self.bundle_id,
            operation=operation,
            data=dict(data),
            tenant=self.tenant,
            project=self.project,
            route=route,
            http_method=http_method,
        )
        return _unwrap_operation_result(operation, result)


def connection_hub_bundle_id_from_entrypoint(entrypoint: Any) -> str:
    return connection_hub_bundle_id(entrypoint)


__all__ = [
    "BundleOperationCaller",
    "DEFAULT_CONNECTION_HUB_BUNDLE_ID",
    "ConnectionEdgesClient",
    "connection_hub_bundle_id",
    "connection_hub_bundle_id_from_entrypoint",
    "is_local_or_internal_host",
    "public_proto",
    "request_origin",
]

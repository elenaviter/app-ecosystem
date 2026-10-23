"""Outbound network policy for user-supplied endpoints.

One implementation for every product that calls a URL a person typed in:
Connection Hub's remote MCP connectors and the A2A client use it. Public HTTPS
is the default. Local and private endpoints need an explicit deployment
allowlist; a user-supplied URL cannot opt itself into that path. Redirects are
a transport concern and are refused by the guarded transport.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

AddressResolver = Callable[[str, int], Awaitable[Iterable[str]]]


class OutboundEndpointDenied(ValueError):
    """The endpoint is outside the deployment's outbound network policy."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def resolve_endpoint_addresses(host: str, port: int) -> tuple[str, ...]:
    def _resolve() -> tuple[str, ...]:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(sorted({str(row[4][0]) for row in rows if row[4]}))

    try:
        return await asyncio.to_thread(_resolve)
    except socket.gaierror as exc:
        raise OutboundEndpointDenied("endpoint_dns_unresolved") from exc


def _address_denied(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global


@dataclass(frozen=True)
class OutboundEndpointPolicy:
    """Deployment-owned outbound policy, evaluated before every connection."""

    allow_http: bool = False
    allow_private_networks: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    resolver: AddressResolver = resolve_endpoint_addresses

    async def connect_addresses(self, host: Any, port: Any) -> tuple[str, ...]:
        """Resolve and authorize the addresses an outbound socket may use."""

        normalized_host = str(host or "").strip().lower().rstrip(".")
        if normalized_host.startswith("[") and normalized_host.endswith("]"):
            normalized_host = normalized_host[1:-1]
        if not normalized_host:
            raise OutboundEndpointDenied("endpoint_host_missing")
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as exc:
            raise OutboundEndpointDenied("endpoint_invalid") from exc
        if normalized_port < 1 or normalized_port > 65535:
            raise OutboundEndpointDenied("endpoint_invalid")

        allowlisted = normalized_host in {
            item.lower().rstrip(".") for item in self.allowed_hosts
        }
        try:
            literal = ipaddress.ip_address(normalized_host)
            addresses = (str(literal),)
        except ValueError:
            addresses = tuple(await self.resolver(normalized_host, normalized_port))
        addresses = tuple(sorted({str(item).strip() for item in addresses if str(item).strip()}))
        if not addresses:
            raise OutboundEndpointDenied("endpoint_dns_unresolved")
        if not (allowlisted or self.allow_private_networks):
            if any(_address_denied(address) for address in addresses):
                raise OutboundEndpointDenied("endpoint_private_network_forbidden")
        return addresses

    async def validate(self, endpoint: Any) -> str:
        raw = str(endpoint or "").strip()
        if not raw or len(raw) > 2048:
            raise OutboundEndpointDenied("endpoint_invalid")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise OutboundEndpointDenied("endpoint_invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in ({"https", "http"} if self.allow_http else {"https"}):
            raise OutboundEndpointDenied("endpoint_scheme_not_allowed")
        if parsed.username is not None or parsed.password is not None:
            raise OutboundEndpointDenied("endpoint_userinfo_forbidden")
        if parsed.fragment:
            raise OutboundEndpointDenied("endpoint_fragment_forbidden")
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise OutboundEndpointDenied("endpoint_host_missing")
        normalized_port = int(port or (443 if scheme == "https" else 80))
        await self.connect_addresses(host, normalized_port)

        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"
        default_port = 443 if scheme == "https" else 80
        if normalized_port != default_port:
            netloc = f"{netloc}:{normalized_port}"
        path = parsed.path or "/"
        return urlunsplit((scheme, netloc, path, parsed.query, ""))


__all__ = [
    "AddressResolver",
    "OutboundEndpointDenied",
    "OutboundEndpointPolicy",
    "resolve_endpoint_addresses",
]

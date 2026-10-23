"""The single outbound endpoint policy and its guarded transport.

``LegacyPolicy`` below is Connection Hub's ``RemoteMCPEndpointPolicy`` as it was
before it moved here (connection_hub/remote_mcp/security.py at b34f3af4),
copied verbatim except for names. The differential table proves the moved
policy decides every case the same way.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import pytest

from app_foundation.net import (
    GuardedNetworkBackend,
    OutboundEndpointDenied,
    OutboundEndpointPolicy,
    guarded_async_transport,
)


class LegacyDenied(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _legacy_address_denied(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global


@dataclass(frozen=True)
class LegacyPolicy:
    allow_http: bool = False
    allow_private_networks: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    resolver: Callable[[str, int], Awaitable[Iterable[str]]] = None  # type: ignore[assignment]

    async def connect_addresses(self, host: Any, port: Any) -> tuple[str, ...]:
        normalized_host = str(host or "").strip().lower().rstrip(".")
        if normalized_host.startswith("[") and normalized_host.endswith("]"):
            normalized_host = normalized_host[1:-1]
        if not normalized_host:
            raise LegacyDenied("endpoint_host_missing")
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as exc:
            raise LegacyDenied("endpoint_invalid") from exc
        if normalized_port < 1 or normalized_port > 65535:
            raise LegacyDenied("endpoint_invalid")
        allowlisted = normalized_host in {item.lower().rstrip(".") for item in self.allowed_hosts}
        try:
            literal = ipaddress.ip_address(normalized_host)
            addresses = (str(literal),)
        except ValueError:
            addresses = tuple(await self.resolver(normalized_host, normalized_port))
        addresses = tuple(sorted({str(item).strip() for item in addresses if str(item).strip()}))
        if not addresses:
            raise LegacyDenied("endpoint_dns_unresolved")
        if not (allowlisted or self.allow_private_networks):
            if any(_legacy_address_denied(address) for address in addresses):
                raise LegacyDenied("endpoint_private_network_forbidden")
        return addresses

    async def validate(self, endpoint: Any) -> str:
        raw = str(endpoint or "").strip()
        if not raw or len(raw) > 2048:
            raise LegacyDenied("endpoint_invalid")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise LegacyDenied("endpoint_invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in ({"https", "http"} if self.allow_http else {"https"}):
            raise LegacyDenied("endpoint_scheme_not_allowed")
        if parsed.username is not None or parsed.password is not None:
            raise LegacyDenied("endpoint_userinfo_forbidden")
        if parsed.fragment:
            raise LegacyDenied("endpoint_fragment_forbidden")
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise LegacyDenied("endpoint_host_missing")
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


ANSWERS = {
    "public.example": ["93.184.216.34"],
    "mixed.example": ["93.184.216.34", "10.0.0.7"],
    "private.example": ["192.168.1.10"],
    "v6.example": ["2606:4700::6810:84e5"],
    "mapped.example": ["::ffff:10.0.0.1"],
    "zero.example": ["0.0.0.0"],
    "xn--bcher-kva.example": ["93.184.216.34"],
    "allowed.internal": ["10.1.2.3"],
}


async def _resolver(host: str, port: int) -> tuple[str, ...]:
    del port
    return tuple(ANSWERS.get(host, ()))


ENDPOINTS = [
    "https://public.example/mcp",
    "https://PUBLIC.example./mcp?x=1",
    "https://public.example:8443/a2a",
    "https://public.example:443/a2a",
    "https://public.example",
    "http://public.example/a2a",
    "https://user:pass@public.example/",
    "https://public.example/#frag",
    "https://mixed.example/",
    "https://private.example/",
    "https://v6.example/",
    "https://mapped.example/",
    "https://zero.example/",
    "https://unknown.example/",
    "https://127.0.0.1/",
    "https://0.0.0.0/",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",
    "https://[2606:4700::6810:84e5]:8443/",
    "https://2130706433/",
    "https://0177.0.0.1/",
    "https://0x7f.0.0.1/",
    "https://xn--bcher-kva.example/",
    "https://bücher.example/",
    "https://allowed.internal/",
    "https://public.example:0/",
    "https://public.example:99999/",
    "https://public.example:abc/",
    "ftp://public.example/",
    "",
    "   ",
    "https://" + "a" * 2050 + ".example/",
    "https:///nohost",
]

POLICY_SHAPES = [
    {},
    {"allow_http": True},
    {"allow_private_networks": True},
    {"allowed_hosts": frozenset({"allowed.internal", "PRIVATE.example."})},
]


async def _outcome(policy: Any, endpoint: str) -> tuple[str, str]:
    try:
        return ("ok", await policy.validate(endpoint))
    except (OutboundEndpointDenied, LegacyDenied) as exc:
        return ("denied", exc.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", POLICY_SHAPES)
async def test_moved_policy_decides_every_case_like_the_remote_mcp_policy(shape: dict[str, Any]) -> None:
    moved = OutboundEndpointPolicy(resolver=_resolver, **shape)
    legacy = LegacyPolicy(resolver=_resolver, **shape)
    mismatches = {}
    for endpoint in ENDPOINTS:
        new, old = await _outcome(moved, endpoint), await _outcome(legacy, endpoint)
        if new != old:
            mismatches[endpoint] = (new, old)
    assert mismatches == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "port"),
    [("public.example", 443), ("mixed.example", 443), ("10.0.0.1", 443), ("[::1]", 443), ("", 443), ("public.example", 0)],
)
async def test_moved_connect_addresses_matches_the_remote_mcp_policy(host: str, port: int) -> None:
    async def outcome(policy: Any) -> tuple[str, Any]:
        try:
            return ("ok", await policy.connect_addresses(host, port))
        except (OutboundEndpointDenied, LegacyDenied) as exc:
            return ("denied", exc.reason)

    assert await outcome(OutboundEndpointPolicy(resolver=_resolver)) == await outcome(LegacyPolicy(resolver=_resolver))


def test_remote_mcp_names_are_the_moved_policy() -> None:
    security = pytest.importorskip("connection_hub.remote_mcp.security")
    assert security.RemoteMCPEndpointPolicy is OutboundEndpointPolicy
    assert security.RemoteMCPEndpointDenied is OutboundEndpointDenied


class _RecordingBackend:
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []
        self.tls_server_names: list[str] = []

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> Any:
        del kwargs
        self.connected.append((host, port))
        return _RecordingStream(self)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unix sockets must never reach the inner backend")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _StopAtTLS(Exception):
    pass


class _RecordingStream:
    def __init__(self, backend: _RecordingBackend) -> None:
        self._backend = backend

    async def start_tls(self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None) -> Any:
        del ssl_context, timeout
        self._backend.tls_server_names.append(str(server_hostname))
        raise _StopAtTLS()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise _StopAtTLS()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        raise _StopAtTLS()

    async def aclose(self) -> None:
        return None

    def get_extra_info(self, info: str) -> Any:
        return None


@pytest.mark.asyncio
async def test_sockets_connect_to_the_checked_address_and_tls_names_the_original_host(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    httpx2 = pytest.importorskip("httpx2")
    policy = OutboundEndpointPolicy(resolver=_resolver)
    transport = guarded_async_transport(policy)
    recorder = _RecordingBackend()
    transport._pool._network_backend._inner = recorder

    async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(Exception):
            await client.get("https://public.example/a2a")

    assert recorder.connected == [("93.184.216.34", 443)]
    assert recorder.tls_server_names == ["public.example"]


@pytest.mark.asyncio
async def test_a_resolver_that_changes_its_answer_cannot_move_the_socket() -> None:
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def rebinding(host: str, port: int) -> list[str]:
        del host, port
        return next(answers)

    policy = OutboundEndpointPolicy(resolver=rebinding)
    recorder = _RecordingBackend()
    backend = GuardedNetworkBackend(inner=recorder, endpoint_policy=policy)

    await policy.validate("https://rebind.example/a2a")  # the URL check sees a public answer
    with pytest.raises(OutboundEndpointDenied) as denied:
        await backend.connect_tcp("rebind.example", 443)  # the socket check sees the private one

    assert denied.value.reason == "endpoint_private_network_forbidden"
    assert recorder.connected == []


@pytest.mark.asyncio
async def test_unix_sockets_are_refused() -> None:
    backend = GuardedNetworkBackend(inner=_RecordingBackend(), endpoint_policy=OutboundEndpointPolicy(resolver=_resolver))
    with pytest.raises(RuntimeError, match="endpoint_unix_socket_forbidden"):
        await backend.connect_unix_socket("/var/run/docker.sock")

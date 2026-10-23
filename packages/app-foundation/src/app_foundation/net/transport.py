"""An HTTP transport whose every socket goes to an address the policy checked.

The policy resolves the host once per connection and authorizes each address;
the socket then connects to that address, so a second DNS answer cannot move
the connection (DNS rebinding). TLS still verifies the certificate against the
original host name: httpcore performs the TLS handshake after ``connect_tcp``
with the request's host as the server name. Proxy variables are ignored
(``trust_env=False``), unix sockets are refused, and the transport never
retries. Redirects are refused by the caller's client (``follow_redirects``
stays off).
"""

from __future__ import annotations

from typing import Any

from app_foundation.net.policy import OutboundEndpointPolicy


class GuardedNetworkBackend:
    """Resolve, validate, and pin the address used by each outbound socket."""

    def __init__(self, *, inner: Any, endpoint_policy: OutboundEndpointPolicy) -> None:
        self._inner = inner
        self._endpoint_policy = endpoint_policy

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        addresses = await self._endpoint_policy.connect_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001 - try the next checked address
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("endpoint_has_no_connectable_address")

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("endpoint_unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_async_transport(endpoint_policy: OutboundEndpointPolicy) -> Any:
    """Return an ``httpx2.AsyncHTTPTransport`` whose sockets the policy pins."""

    import httpx2

    transport = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)
    pool = getattr(transport, "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if pool is None or inner is None:
        raise RuntimeError("guarded_transport_unavailable")
    pool._network_backend = GuardedNetworkBackend(inner=inner, endpoint_policy=endpoint_policy)
    return transport


__all__ = ["GuardedNetworkBackend", "guarded_async_transport"]

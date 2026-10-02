"""One reused HTTP client per event loop for OAuth requests (W461 follow-up).

On 2026-10-02 every OAuth request opened its own ``httpx2.AsyncClient``, so
each paid DNS, TCP and TLS again. Exact request-id joins between the relay's
request records and the web proxy showed about 1.5 s per metadata request
spent outside the server on the development host, and a token refresh makes
about four such requests. The public address is kept by ruling (every client
reaches Connection Hub as any client does), so the remedy is reuse: one
client, with keep-alive connections, per running event loop.

A caller that injects its own transport (tests) keeps a client of its own
per request, as before. The shared client keeps no cookies, so nothing one
identity's response sets reaches another identity's request. It is not
closed explicitly: it lives as long as its event loop, and a one-shot
command's loop ends with the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import weakref
from typing import Any, AsyncIterator

# Idle pooled connections live this long; a token refresh's requests follow
# each other within seconds, and relay refreshes recur within minutes.
KEEPALIVE_SECONDS = 60.0
MAX_KEEPALIVE_CONNECTIONS = 20

_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def _no_cookies() -> http.cookiejar.CookieJar:
    """A cookie jar that accepts no cookie from any domain."""

    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def _new_pooled_client() -> Any:
    import httpx2

    return httpx2.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        # The client is shared by every profile, account and host on the
        # loop: a cookie one response sets must never ride another identity's
        # request (W461 review), so the jar stores nothing.
        cookies=_no_cookies(),
        limits=httpx2.Limits(
            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=KEEPALIVE_SECONDS,
        ),
    )


def pooled_client() -> Any:
    """The running loop's shared client, made on first use."""

    loop = asyncio.get_running_loop()
    client = _CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = _new_pooled_client()
        _CLIENTS[loop] = client
    return client


@contextlib.asynccontextmanager
async def oauth_http_client(*, transport: Any, timeout_seconds: float) -> AsyncIterator[Any]:
    """The client for one OAuth request: the loop's shared one, or a new one for an injected transport."""

    if transport is None:
        yield pooled_client()
        return
    import httpx2

    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(timeout_seconds),
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    ) as client:
        yield client

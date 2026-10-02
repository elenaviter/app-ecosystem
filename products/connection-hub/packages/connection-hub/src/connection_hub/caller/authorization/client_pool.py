"""One reused HTTP client per event loop for OAuth requests (W461 follow-up).

On 2026-10-02 every OAuth request opened its own ``httpx2.AsyncClient`` and
so paid DNS, TCP and TLS again, and a token refresh makes about four
requests. The public address is kept by ruling (every client reaches
Connection Hub as any client does), so the remedy is reuse: one client, with
keep-alive connections, per running event loop (W464). A curl probe of the
public route measured DNS, TCP and TLS together at 0.1 to 0.3 s, so reuse
saves that per request; the larger gap seen outside the server on the
development host (about 1.5 s per metadata request) is attributed by the
phases in each request record, not assumed to be the handshake.

The pool holds at most ``MAX_CONNECTIONS`` connections per loop. A request
beyond them waits for one inside its own timeout (the pool wait is part of
``httpx2.Timeout``), and that wait shows as the request's ``queue_ms``.

A caller that injects its own transport (tests) keeps a client of its own
per request, as before. The shared client keeps no cookies, so nothing one
identity's response sets reaches another identity's request. It sets no
header, credential or base URL of its own: everything identity-bound travels
on the request. ``close_pooled_client`` closes the running loop's client and
drops it, and the next request makes a new one; without it the client lives
as long as its loop, and the process ending closes its sockets.
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
MAX_CONNECTIONS = 20

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
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_CONNECTIONS,
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


async def close_pooled_client() -> None:
    """Close the running loop's shared client, if any; the next request makes a new one."""

    client = _CLIENTS.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


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

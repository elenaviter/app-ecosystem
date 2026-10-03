"""OAuth requests reuse one HTTP client per event loop (W461 follow-up)."""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.caller.authorization import client_pool, discovery

httpx2 = pytest.importorskip("httpx2")


@pytest.fixture
def counted_pool(monkeypatch):
    made: list[object] = []
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers[discovery.REQUEST_ID_HEADER])
        return httpx2.Response(200, json={"ok": True})

    def new_client():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False, trust_env=False)
        made.append(client)
        return client

    monkeypatch.setattr(client_pool, "_new_pooled_client", new_client)
    monkeypatch.setattr(client_pool, "_CLIENTS", __import__("weakref").WeakKeyDictionary())
    return made, seen


def test_requests_on_one_loop_share_one_client(counted_pool):
    made, seen = counted_pool
    transport = discovery.HttpxOAuthTransport(timeout_seconds=2)

    async def scenario():
        await transport.get_json("https://hub.example/.well-known/oauth-protected-resource")
        await transport.get_json("https://hub.example/.well-known/oauth-authorization-server")
        await transport.post_form("https://hub.example/oauth/token", {"grant_type": "refresh_token"})

    asyncio.run(scenario())
    assert len(made) == 1, "one client for the loop, not one per request"
    assert len(seen) == 3 and len(set(seen)) == 3, "each request still has its own request id"


def test_a_new_loop_gets_its_own_client(counted_pool):
    made, _ = counted_pool
    transport = discovery.HttpxOAuthTransport(timeout_seconds=2)
    asyncio.run(transport.get_json("https://hub.example/a"))
    asyncio.run(transport.get_json("https://hub.example/b"))
    assert len(made) == 2, "a client is bound to its event loop"


def test_an_injected_transport_keeps_a_client_per_request(monkeypatch):
    def fail():
        raise AssertionError("the shared pool is not used for an injected transport")

    monkeypatch.setattr(client_pool, "_new_pooled_client", fail)
    transport = discovery.HttpxOAuthTransport(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})), timeout_seconds=2
    )
    assert asyncio.run(transport.get_json("https://hub.example/a")) == {}


def test_the_shared_client_carries_no_cookie_from_one_request_to_the_next(monkeypatch):
    """Ops on PR 439: one client serves every profile on the loop, so it must keep no cookie."""

    sent_cookies: list[str] = []

    def handler(request):
        sent_cookies.append(request.headers.get("cookie", ""))
        return httpx2.Response(200, json={}, headers={"set-cookie": "sess=profileA; Path=/"})

    real_new = client_pool._new_pooled_client

    def new_client():
        client = real_new()
        client._transport = httpx2.MockTransport(handler)
        return client

    monkeypatch.setattr(client_pool, "_new_pooled_client", new_client)
    monkeypatch.setattr(client_pool, "_CLIENTS", __import__("weakref").WeakKeyDictionary())
    transport = discovery.HttpxOAuthTransport(timeout_seconds=2)

    async def scenario():
        await transport.get_json("https://hub.example/a")
        await transport.get_json("https://hub.example/b")

    asyncio.run(scenario())
    assert sent_cookies == ["", ""], "a cookie set by one response rode the next request"

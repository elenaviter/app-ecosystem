"""Each OAuth request record says where its client-side time went, on the real pooled client (W464).

The server here is a synthetic HTTP/1.1 server on the loopback address; the
client side is the real shared client of ``client_pool`` with its real
connection pool, so reuse, the connection cap, cancellation and the trace
phases are the library's own behaviour, not a mock's.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import weakref

import pytest

from connection_hub.caller.authorization import client_pool, discovery, request_records
from connection_hub.caller.errors import AuthorizationError

httpx2 = pytest.importorskip("httpx2")

PROFILE_A = "problem-board-claude-aaaaaaaaaaaa"
PROFILE_B = "problem-board-codex-bbbbbbbbbbbb"


class LoopbackServer:
    """Counts connections and in-flight requests; can hold every response until released."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.release = asyncio.Event()
        self.connections = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.requests: list[dict[str, str]] = []
        self.bodies: list[bytes] = []
        self.arrived = asyncio.Event()

    async def __aenter__(self) -> "LoopbackServer":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.release.set()
        self._server.close()
        await self._server.wait_closed()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")[1:]
                headers = {}
                for line in lines:
                    if line:
                        name, _, value = line.partition(":")
                        headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                self.bodies.append(await reader.readexactly(length) if length else b"")
                self.requests.append(headers)
                self.arrived.set()
                self.in_flight += 1
                self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
                try:
                    if self.hold:
                        await self.release.wait()
                finally:
                    self.in_flight -= 1
                body = b'{"ok": true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + b"Content-Length: %d\r\n\r\n" % len(body)
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture(autouse=True)
def fresh_pool(monkeypatch):
    monkeypatch.setattr(client_pool, "_CLIENTS", weakref.WeakKeyDictionary())


def _records(caplog) -> list[dict[str, str]]:
    return [
        dict(re.findall(r"(\w+)=(\S+)", r.getMessage()))
        for r in caplog.records
        if r.name == "connection_hub.oauth.requests"
    ]


def _transport() -> discovery.HttpxOAuthTransport:
    return discovery.HttpxOAuthTransport(timeout_seconds=5)


def test_the_first_request_connects_and_the_next_ones_reuse_the_connection(caplog):
    async def scenario():
        async with LoopbackServer() as server:
            transport = _transport()
            await transport.get_json(server.url("/.well-known/oauth-protected-resource"))
            await transport.get_json(server.url("/.well-known/oauth-authorization-server"))
            await transport.post_form(server.url("/oauth/token"), {"grant_type": "refresh_token"})
            await client_pool.close_pooled_client()
            return server.connections

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.requests"):
        connections = asyncio.run(scenario())
    first, second, third = _records(caplog)
    assert connections == 1, "three requests on one kept-alive connection"
    assert first["connect_ms"] != "-", "the first request opened the connection"
    assert second["connect_ms"] == "-" and third["connect_ms"] == "-", "the next ones reused it"
    assert first["tls_ms"] == "-", "plain HTTP on loopback has no TLS phase"
    for record in (first, second, third):
        for field in ("queue_ms", "send_ms", "wait_ms", "read_ms"):
            assert record[field].isdigit(), (field, record)
        assert record["failed_at"] == "-"


def test_the_pool_caps_connections_and_the_wait_is_the_request_queue_time(monkeypatch, caplog):
    monkeypatch.setattr(client_pool, "MAX_CONNECTIONS", 2)

    async def scenario():
        async with LoopbackServer(hold=True) as server:
            transport = _transport()
            tasks = [asyncio.create_task(transport.get_json(server.url(f"/r{index}"))) for index in range(3)]
            while len(server.requests) < 2:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
            waiting = len(server.requests)
            server.release.set()
            await asyncio.gather(*tasks)
            await client_pool.close_pooled_client()
            return waiting, server.connections, server.peak_in_flight

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.requests"):
        waiting, connections, peak = asyncio.run(scenario())
    assert waiting == 2, "the third request waited for a pooled connection"
    assert connections == 2 and peak == 2
    queued = max(int(record["queue_ms"]) for record in _records(caplog))
    assert queued >= 250, "the pool wait shows as the waiting request's queue time"


def test_a_refused_connection_names_the_connect_phase(caplog):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def scenario():
        with pytest.raises(AuthorizationError):
            await _transport().get_json(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource")
        await client_pool.close_pooled_client()

    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.requests"):
        asyncio.run(scenario())
    (record,) = _records(caplog)
    assert (record["outcome"], record["failed_at"]) == ("oauth_metadata_request_failed", "connect")


def test_a_cancel_while_waiting_for_the_answer_names_it_and_the_shared_client_still_works(caplog):
    async def scenario():
        async with LoopbackServer(hold=True) as server:
            transport = _transport()
            task = asyncio.create_task(transport.get_json(server.url("/slow")))
            await server.arrived.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.hold = False
            server.release.set()
            assert await transport.get_json(server.url("/after")) == {"ok": True}
            await client_pool.close_pooled_client()

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.requests"):
        asyncio.run(scenario())
    cancelled, after = _records(caplog)
    assert (cancelled["outcome"], cancelled["failed_at"]) == ("cancelled", "wait")
    assert (after["outcome"], after["failed_at"]) == ("ok", "-")


def test_requests_of_two_profiles_on_one_connection_carry_only_their_own_data(caplog):
    async def scenario():
        async with LoopbackServer() as server:
            transport = _transport()
            with request_records.correlate(PROFILE_A):
                await transport.post_form(server.url("/oauth/token"), {"refresh_token": "CANARY-A"})
            with request_records.correlate(PROFILE_B):
                await transport.post_form(server.url("/oauth/token"), {"refresh_token": "CANARY-B"})
            await client_pool.close_pooled_client()
            return server

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.requests"):
        server = asyncio.run(scenario())
    assert server.connections == 1
    assert b"CANARY-A" in server.bodies[0] and b"CANARY-B" not in server.bodies[0]
    assert b"CANARY-B" in server.bodies[1] and b"CANARY-A" not in server.bodies[1]
    for headers in server.requests:
        assert "cookie" not in headers and "authorization" not in headers
    a, b = _records(caplog)
    assert a["corr"] != b["corr"] and a["profile"] != b["profile"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    for canary in ("CANARY-A", "CANARY-B", PROFILE_A, PROFILE_B, "127.0.0.1", "/oauth/token"):
        assert canary not in text, canary


def test_close_drops_the_loop_client_and_the_next_request_makes_a_new_one():
    async def scenario():
        async with LoopbackServer() as server:
            transport = _transport()
            await transport.get_json(server.url("/a"))
            first = client_pool.pooled_client()
            await client_pool.close_pooled_client()
            assert first.is_closed
            await transport.get_json(server.url("/b"))
            second = client_pool.pooled_client()
            await client_pool.close_pooled_client()
            await client_pool.close_pooled_client()  # nothing left to close
            return first is not second, server.connections

    replaced, connections = asyncio.run(scenario())
    assert replaced and connections == 2


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_phases_sum_the_library_steps_by_phase():
    clock = Clock()
    phases = request_records.RequestPhases(100.0, clock=clock)
    for at, event in [
        (100.2, "connection.connect_tcp.started"),
        (100.5, "connection.connect_tcp.complete"),
        (100.5, "connection.start_tls.started"),
        (100.9, "connection.start_tls.complete"),
        (100.9, "http11.send_request_headers.started"),
        (101.0, "http11.send_request_headers.complete"),
        (101.0, "http11.send_request_body.started"),
        (101.1, "http11.send_request_body.complete"),
        (101.1, "http11.receive_response_headers.started"),
        (102.6, "http11.receive_response_headers.complete"),
        (102.6, "http11.receive_response_body.started"),
        (102.7, "http11.receive_response_body.complete"),
        (102.7, "http11.response_closed.started"),
    ]:
        clock.now = at
        phases.observe(event)
    assert round(phases.queue_seconds(), 3) == 0.2
    assert {name: round(value, 3) for name, value in phases.seconds.items()} == {
        "connect": 0.3, "tls": 0.4, "send": 0.2, "wait": 1.5, "read": 0.1,
    }
    assert phases.failed_at == "-"


def test_a_request_that_never_reached_the_network_has_no_phases(caplog):
    with caplog.at_level(logging.WARNING, logger="connection_hub.oauth.requests"):
        request_records.record(
            request_id="r", kind="token", method="POST", status=None, outcome="cancelled",
            elapsed_seconds=0.01, phases=request_records.RequestPhases(0.0),
        )
    (record,) = _records(caplog)
    assert [record[f] for f in ("queue_ms", "connect_ms", "tls_ms", "send_ms", "wait_ms", "read_ms", "failed_at")] == ["-"] * 7

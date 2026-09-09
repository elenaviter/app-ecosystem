# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The one public-origin derivation every surface links and calls back through."""

from __future__ import annotations

import pytest

from connection_hub.connection_edges import (
    is_local_or_internal_host,
    public_proto,
    request_origin,
)


class _URL:
    def __init__(self, scheme: str, netloc: str) -> None:
        self.scheme = scheme
        self.netloc = netloc


class _Request:
    """A request as it reaches the proc: the connection is plain http, and the
    public scheme is whatever the deployment's proxy declared."""

    def __init__(self, headers, scheme="http", netloc="chat-proc:8010"):
        self.headers = headers
        self.url = _URL(scheme, netloc)
        self.base_url = f"{scheme}://{netloc}/"


def test_forwarded_proto_and_host_win_over_every_other_source():
    origin = request_origin(
        _Request({"host": "chat-proc:8010", "x-forwarded-proto": "http",
                  "forwarded": "proto=https;host=demo.kdcube.tech"})
    )
    assert origin == "https://demo.kdcube.tech"


def test_x_forwarded_proto_is_taken_when_there_is_no_forwarded_header():
    assert request_origin(
        _Request({"host": "wildcat.ngrok-free.dev", "x-forwarded-proto": "https"})
    ) == "https://wildcat.ngrok-free.dev"


def test_a_loopback_origin_keeps_the_scheme_the_request_arrived_on():
    """A callback minted as https for a port with no TLS is unreachable, and the
    upstream OAuth flow fails on it with a TLS version error."""
    assert request_origin(_Request({"host": "localhost:8020"})) == "http://localhost:8020"
    assert request_origin(_Request({"host": "127.0.0.1:8020"})) == "http://127.0.0.1:8020"
    assert request_origin(_Request({"host": "box.local:8020"})) == "http://box.local:8020"


def test_a_public_host_reached_over_http_is_treated_as_https():
    """No forwarded provenance on a public name means a terminator dropped it."""
    assert request_origin(_Request({"host": "demo.kdcube.tech"})) == "https://demo.kdcube.tech"


def test_the_connection_scheme_is_used_when_no_header_carries_a_host():
    assert request_origin(_Request({}, scheme="https", netloc="demo.kdcube.tech")) == (
        "https://demo.kdcube.tech"
    )


def test_no_request_and_no_headers_yield_no_origin():
    assert request_origin(None) == ""


@pytest.mark.parametrize(
    "host,local",
    [
        ("localhost:8020", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]:8020", True),
        ("box.local", True),
        ("chat-proc", True),          # single label: no public authority
        ("demo.kdcube.tech", False),
        ("wildcat.ngrok-free.dev", False),
    ],
)
def test_local_hosts_are_told_from_public_ones(host, local):
    assert is_local_or_internal_host(host) is local


def test_public_proto_never_downgrades_and_never_invents_https_locally():
    assert public_proto("https", "localhost") == "https"
    assert public_proto("http", "localhost") == "http"
    assert public_proto("http", "demo.kdcube.tech") == "https"
    assert public_proto("", "localhost") == "http"


def test_public_proto_rejects_non_http_schemes():
    assert public_proto("javascript", "localhost") == "http"
    assert public_proto("javascript", "demo.kdcube.tech") == "https"
    assert request_origin(
        _Request({"host": "localhost:8020", "x-forwarded-proto": "javascript"})
    ) == "http://localhost:8020"

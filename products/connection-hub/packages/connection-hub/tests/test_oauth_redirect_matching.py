# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Native loopback redirects match exactly except for the port (RFC 8252 section 8.4)."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.oauth.clients import (
    CLIENT_REGISTRATION_DYNAMIC,
    CLIENT_REGISTRATION_METADATA_DOCUMENT,
    CLIENT_REGISTRATION_PRE_REGISTERED,
    PublicClient,
    dcr_redirect_allowed,
    get_client,
    redirect_uri_allowed,
)

REGISTERED = "http://127.0.0.1/callback?mode=cli"


def _client(*redirects: str, kind: str = CLIENT_REGISTRATION_PRE_REGISTERED) -> PublicClient:
    return PublicClient(client_id="native-test", redirect_uris=redirects, registration_kind=kind)


@pytest.mark.parametrize("kind", [CLIENT_REGISTRATION_PRE_REGISTERED, CLIENT_REGISTRATION_DYNAMIC])
@pytest.mark.parametrize(
    "requested",
    [
        REGISTERED,
        "http://127.0.0.1:53127/callback?mode=cli",
        "http://127.0.0.1:1/callback?mode=cli",
        "http://127.0.0.1:65535/callback?mode=cli",
    ],
)
def test_port_is_the_only_permitted_variation(kind: str, requested: str) -> None:
    assert redirect_uri_allowed(_client(REGISTERED, kind=kind), requested)


@pytest.mark.parametrize("kind", [CLIENT_REGISTRATION_PRE_REGISTERED, CLIENT_REGISTRATION_DYNAMIC])
@pytest.mark.parametrize(
    "requested",
    [
        pytest.param("http://127.0.0.1:53127/callback?mode=other", id="changed-query"),
        pytest.param("http://127.0.0.1:53127/callback?mode=cli&x=1", id="added-query"),
        pytest.param("http://127.0.0.1:53127/callback", id="removed-query"),
        pytest.param("http://127.0.0.1:53127/callback?mode=CLI", id="query-case"),
        pytest.param("http://127.0.0.1:53127/callback?mode=%63li", id="query-encoding"),
        pytest.param("http://127.0.0.1:53127/callback?mode=cli#frag", id="fragment"),
        pytest.param("http://127.0.0.1:53127/callback?mode=cli#", id="empty-fragment"),
        pytest.param("http://user:pw@127.0.0.1:53127/callback?mode=cli", id="userinfo"),
        pytest.param("http://user@127.0.0.1:53127/callback?mode=cli", id="username"),
        pytest.param("http://127.0.0.1:99999/callback?mode=cli", id="port-out-of-range"),
        pytest.param("http://127.0.0.1:abc/callback?mode=cli", id="port-not-numeric"),
        pytest.param("http://127.0.0.1:0/callback?mode=cli", id="port-zero"),
        pytest.param("http://127.0.0.1:/callback?mode=cli", id="port-empty"),
        pytest.param("http://127.0.0.1:53127/other?mode=cli", id="changed-path"),
        pytest.param("http://127.0.0.1:53127/callback/?mode=cli", id="trailing-slash"),
        pytest.param("https://127.0.0.1:53127/callback?mode=cli", id="changed-scheme"),
        pytest.param("http://localhost:53127/callback?mode=cli", id="changed-host"),
        pytest.param("http://127.0.0.1.evil.example:53127/callback?mode=cli", id="lookalike-host"),
        pytest.param(" http://127.0.0.1:53127/callback?mode=cli", id="leading-space"),
    ],
)
def test_any_other_difference_is_refused(kind: str, requested: str) -> None:
    assert not redirect_uri_allowed(_client(REGISTERED, kind=kind), requested)


def test_ipv6_loopback_varies_only_by_port() -> None:
    client = _client("http://[::1]/callback")
    assert redirect_uri_allowed(client, "http://[::1]:53127/callback")
    assert not redirect_uri_allowed(client, "http://[::1]:53127/callback?x=1")


def test_portless_metadata_document_redirect_allows_runtime_port() -> None:
    client = _client("http://127.0.0.1/callback", kind=CLIENT_REGISTRATION_METADATA_DOCUMENT)
    assert redirect_uri_allowed(client, "http://127.0.0.1:41002/callback")
    assert not redirect_uri_allowed(client, "http://127.0.0.1:41002/callback?x=1")


def test_explicit_port_metadata_document_redirect_is_exact() -> None:
    client = _client("http://127.0.0.1:41001/callback", kind=CLIENT_REGISTRATION_METADATA_DOCUMENT)
    assert redirect_uri_allowed(client, "http://127.0.0.1:41001/callback")
    assert not redirect_uri_allowed(client, "http://127.0.0.1:41002/callback")
    assert not redirect_uri_allowed(client, "http://127.0.0.1/callback")


def test_web_client_redirect_is_exact() -> None:
    client = PublicClient(
        client_id="web-test",
        redirect_uris=("http://127.0.0.1/callback",),
        application_type="web",
    )
    assert redirect_uri_allowed(client, "http://127.0.0.1/callback")
    assert not redirect_uri_allowed(client, "http://127.0.0.1:53127/callback")


def test_claude_redirects_keep_their_published_forms() -> None:
    claude = get_client("claude")
    assert redirect_uri_allowed(claude, "https://claude.ai/api/mcp/auth_callback")
    assert redirect_uri_allowed(claude, "http://127.0.0.1:54321/callback")
    assert redirect_uri_allowed(claude, "http://localhost:8765/callback")
    assert not redirect_uri_allowed(claude, "https://claude.ai:9999/api/mcp/auth_callback")
    assert not redirect_uri_allowed(claude, "http://127.0.0.1:54321/callback?next=/admin")


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("http://127.0.0.1:5000/callback", True),
        ("http://localhost:5000/callback", True),
        ("http://127.0.0.1:5000/callback?x=1", False),
        ("http://127.0.0.1:5000/callback#f", False),
        ("http://user@127.0.0.1:5000/callback", False),
    ],
)
def test_dynamic_registration_admits_only_the_exact_loopback_allowlist(uri: str, allowed: bool) -> None:
    assert dcr_redirect_allowed(uri) is allowed

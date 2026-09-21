"""A rate-limited OAuth request says how long to wait, and discovery is not repeated.

2026-09-21: every gateway 429 carried ``retry_after: 3600``. The relay could
not see it, retried into a bucket it had already exceeded, and repeated both
metadata GETs on every retry.
"""

from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from connection_hub_cli.authorization.discovery import (
    MAX_RETRY_AFTER_SECONDS,
    HttpxOAuthTransport,
    OAuthDiscovery,
)
from connection_hub_cli.errors import AuthorizationError

RESOURCE_METADATA = (
    "https://runtime.example.test/.well-known/oauth-protected-resource"
    "?resource=https%3A%2F%2Fruntime.example.test%2Fmanagement"
)
SERVER_METADATA = "https://auth.example.test/.well-known/oauth-authorization-server"


async def _refused(response):
    import httpx2

    transport = HttpxOAuthTransport(transport=httpx2.MockTransport(lambda _r: response))
    with pytest.raises(AuthorizationError) as raised:
        await transport.get_json(RESOURCE_METADATA)
    return raised.value


@pytest.mark.asyncio
async def test_a_retry_after_header_in_seconds_travels_with_the_failure() -> None:
    import httpx2

    error = await _refused(httpx2.Response(429, headers={"Retry-After": "120"}, json={}))
    assert error.status == 429
    assert error.details["retry_after_seconds"] == 120


@pytest.mark.asyncio
async def test_the_gateway_body_retry_after_is_read_when_no_header_is_sent() -> None:
    import httpx2

    error = await _refused(
        httpx2.Response(429, json={"detail": "Hourly limit exceeded", "retry_after": 3600})
    )
    assert error.details["retry_after_seconds"] == 3600
    assert error.details["server_reason"] == "Hourly limit exceeded"


@pytest.mark.asyncio
async def test_an_http_date_and_a_hostile_value_are_bounded() -> None:
    import httpx2

    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
    dated = await _refused(httpx2.Response(503, headers={"Retry-After": later}, json={}))
    assert 290 <= dated.details["retry_after_seconds"] <= 300
    hostile = await _refused(httpx2.Response(429, headers={"Retry-After": "999999999"}, json={}))
    assert hostile.details["retry_after_seconds"] == MAX_RETRY_AFTER_SECONDS
    silent = await _refused(httpx2.Response(503, json={}))
    assert "retry_after_seconds" not in silent.details


class _CountingTransport:
    def __init__(self, fail_server_with=None) -> None:
        self.gets: list[str] = []
        self.fail_server_with = fail_server_with

    async def get_json(self, url: str):
        self.gets.append(url)
        if url == RESOURCE_METADATA:
            return {
                "resource": "https://runtime.example.test/management",
                "authorization_servers": ["https://auth.example.test"],
            }
        if self.fail_server_with is not None:
            raise self.fail_server_with
        return {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/oauth/authorize",
            "token_endpoint": "https://auth.example.test/oauth/token",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }


@pytest.mark.asyncio
async def test_discovery_is_fetched_once_per_process_within_its_ttl() -> None:
    first = _CountingTransport()
    second = _CountingTransport()
    for transport in (first, second):
        await OAuthDiscovery(transport=transport).discover(
            protected_resource_metadata_url=RESOURCE_METADATA,
            expected_resource="https://runtime.example.test/management",
        )

    assert first.gets == [RESOURCE_METADATA, SERVER_METADATA]
    assert second.gets == [], "the second discovery was served from the cache"


@pytest.mark.asyncio
async def test_a_rate_limited_server_candidate_is_not_folded_into_unavailable() -> None:
    limited = AuthorizationError("oauth_metadata_request_failed", "HTTP 429")
    limited.status = 429
    limited.details = {"status": 429, "retry_after_seconds": 3600}
    transport = _CountingTransport(fail_server_with=limited)

    with pytest.raises(AuthorizationError) as raised:
        await OAuthDiscovery(transport=transport).discover(
            protected_resource_metadata_url=RESOURCE_METADATA,
            expected_resource="https://runtime.example.test/management",
        )

    assert raised.value.status == 429
    assert raised.value.details["retry_after_seconds"] == 3600
    assert transport.gets.count(SERVER_METADATA) <= 1
    assert len(transport.gets) == 2, "no further candidate was tried"

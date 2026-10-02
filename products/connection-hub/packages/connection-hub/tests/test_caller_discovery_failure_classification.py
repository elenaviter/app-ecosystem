"""A metadata request that got no answer is not reported as absent metadata (W461).

2026-10-02 02:58-03:00 UTC: a relay channel reopening during a host stall
timed out on the protected-resource metadata its challenge named, fell through
to the site-root well-known URL, which this deployment answers with 404, and
reported ``oauth_resource_metadata_unavailable``. The timeout was lost, and the
code read as a deployment without metadata rather than a request that did not
complete.
"""

from __future__ import annotations

import pytest

from connection_hub.caller.authorization.discovery import (
    McpOAuthEndpointDiscovery,
    clear_discovery_cache,
)
from connection_hub.caller.errors import AuthorizationError

ENDPOINT = "https://rt.example.test/api/mcp/board"
CHALLENGE = "https://rt.example.test/meta/challenge"
PATH_WELL_KNOWN = "https://rt.example.test/.well-known/oauth-protected-resource/api/mcp/board"
ROOT_WELL_KNOWN = "https://rt.example.test/.well-known/oauth-protected-resource"
SERVER_METADATA = "https://auth.example.test/.well-known/oauth-authorization-server"

RESOURCE = {
    "resource": ENDPOINT,
    "authorization_servers": ["https://auth.example.test"],
}
SERVER = {
    "issuer": "https://auth.example.test",
    "authorization_endpoint": "https://auth.example.test/oauth/authorize",
    "token_endpoint": "https://auth.example.test/oauth/token",
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_discovery_cache()
    yield
    clear_discovery_cache()


def _failure(url: str, *, status: int | None = None, kind: str = "") -> AuthorizationError:
    error = AuthorizationError("oauth_metadata_request_failed", f"OAuth metadata GET {url} failed.")
    error.status = status
    error.details = {"method": "GET", "url": url}
    if status is not None:
        error.details["status"] = status
    if kind:
        error.details["failure_kind"] = kind
    return error


class _Metadata:
    """Answers each metadata URL from a table: a payload or an error to raise."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = {SERVER_METADATA: SERVER, **answers}
        self.gets: list[str] = []

    async def get_json(self, url: str):
        self.gets.append(url)
        answer = self.answers.get(url, _failure(url, status=404))
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _challenge_transport():
    import httpx2

    return httpx2.MockTransport(
        lambda _request: httpx2.Response(
            401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{CHALLENGE}"'}
        )
    )


async def _discover(metadata: _Metadata, *, http_transport=None):
    discovery = McpOAuthEndpointDiscovery(
        transport=metadata, http_transport=http_transport or _challenge_transport()
    )
    return await discovery.discover(ENDPOINT)


@pytest.mark.asyncio
async def test_a_timed_out_candidate_is_reported_through_a_later_root_404() -> None:
    metadata = _Metadata({CHALLENGE: _failure(CHALLENGE, kind="ReadTimeout")})

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.code == "oauth_metadata_request_failed"
    assert raised.value.details["failure_kind"] == "ReadTimeout"
    assert raised.value.details["url"] == CHALLENGE
    assert metadata.gets == [CHALLENGE, PATH_WELL_KNOWN, ROOT_WELL_KNOWN], "candidate order kept"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 500, 502, 503])
async def test_a_server_error_or_request_timeout_is_not_absence(status: int) -> None:
    metadata = _Metadata({CHALLENGE: _failure(CHALLENGE, status=status)})

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.code == "oauth_metadata_request_failed"
    assert raised.value.status == status


@pytest.mark.asyncio
async def test_a_later_candidate_that_answers_still_wins() -> None:
    metadata = _Metadata(
        {
            CHALLENGE: _failure(CHALLENGE, status=503),
            PATH_WELL_KNOWN: RESOURCE,
        }
    )

    result = await _discover(metadata)

    assert result.protected_resource_metadata_url == PATH_WELL_KNOWN
    assert result.protected_resource.resource == ENDPOINT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_metadata_absent_everywhere_is_still_unavailable(status: int) -> None:
    metadata = _Metadata(
        {url: _failure(url, status=status) for url in (CHALLENGE, PATH_WELL_KNOWN, ROOT_WELL_KNOWN)}
    )

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.code == "oauth_resource_metadata_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_refusal_is_the_answer_not_an_absence_to_skip(status: int) -> None:
    metadata = _Metadata(
        {
            CHALLENGE: _failure(CHALLENGE, status=status),
            PATH_WELL_KNOWN: RESOURCE,
        }
    )

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.status == status
    assert metadata.gets == [CHALLENGE], "no later candidate replaced the refusal"


@pytest.mark.asyncio
async def test_a_malformed_document_is_not_skipped() -> None:
    malformed = AuthorizationError("oauth_response_invalid", "The OAuth server returned an invalid JSON response.")
    metadata = _Metadata({CHALLENGE: malformed, PATH_WELL_KNOWN: RESOURCE})

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.code == "oauth_response_invalid"
    assert metadata.gets == [CHALLENGE]


@pytest.mark.asyncio
async def test_metadata_for_another_resource_is_still_refused_after_a_transient_candidate() -> None:
    other = {**RESOURCE, "resource": "https://elsewhere.example.test/api/mcp/board"}
    metadata = _Metadata({CHALLENGE: _failure(CHALLENGE, kind="ConnectError"), PATH_WELL_KNOWN: other})

    with pytest.raises(AuthorizationError) as raised:
        await _discover(metadata)

    assert raised.value.code == "oauth_resource_mismatch"


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_names_the_failure_class_without_its_text() -> None:
    import httpx2

    def timed_out(request):
        raise httpx2.ReadTimeout("read timed out on https://rt.example.test/api/mcp/board?token=secret", request=request)

    with pytest.raises(AuthorizationError) as raised:
        await _discover(_Metadata({}), http_transport=httpx2.MockTransport(timed_out))

    error = raised.value
    assert error.code == "oauth_mcp_endpoint_unreachable"
    assert error.details["failure_kind"] == "ReadTimeout"
    assert error.details["phase"] == "mcp_probe"
    assert error.details["url"] == ENDPOINT
    assert "secret" not in str(error) and "secret" not in repr(error.details)

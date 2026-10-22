"""A refused token request shows the server's description when it is safe to show (W929).

The token step of an authorization-code exchange answered 503
``temporarily_unavailable`` with the description "Original credential exchange
unavailable.", and the CLI printed only the code. The fault was deterministic
but looked transient. The description now travels with the error, unless it
is not printable, too long, or echoes a value the request submitted.
"""

from __future__ import annotations

import json

import pytest

from connection_hub.caller.authorization import discovery
from connection_hub.caller.errors import AuthorizationError

httpx2 = pytest.importorskip("httpx2")

URL = "https://auth.example.test/oauth/token"
SUBMITTED = {"grant_type": "authorization_code", "code": "CANARY-CODE-123456", "client_id": "unit-client-1",
             "code_verifier": "CANARY-VERIFIER-abcdef"}


async def _refused(body: dict, status: int = 503) -> AuthorizationError:
    def handler(request):
        return httpx2.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    transport = discovery.HttpxOAuthTransport(transport=httpx2.MockTransport(handler), timeout_seconds=2)
    with pytest.raises(AuthorizationError) as raised:
        await transport.post_form(URL, SUBMITTED)
    return raised.value


@pytest.mark.asyncio
async def test_a_safe_description_travels_with_the_registered_code():
    error = await _refused({"error": "temporarily_unavailable",
                            "error_description": "Original credential exchange unavailable."})
    assert error.code == "oauth_token_request_failed"
    assert error.details["oauth_error"] == "temporarily_unavailable"
    assert error.details["server_reason"] == "temporarily_unavailable (Original credential exchange unavailable.)"
    assert "Original credential exchange unavailable." in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [
    "echoes CANARY-CODE-123456 back",       # a submitted value
    "code CANARY-CODE-1234 expired",        # a prefix of a submitted value
    "bad canary-verifier value",            # part of one, in another case
    "got CANARY%2DCODE%2D12 here",          # percent-encoded part of one
    "token ABCdef0123456789xyzQ rejected",  # an opaque run, whatever it is
    "verifier CANARY-VERIFIER-abcdef",      # another submitted value
    "line one\nline two",                   # not printable on one line
    "x" * (discovery.MAX_TOKEN_ERROR_DESCRIPTION + 1),
    "",
])
async def test_an_unsafe_description_is_dropped_and_the_code_still_travels(description):
    error = await _refused({"error": "invalid_grant", "error_description": description}, status=400)
    assert error.details["server_reason"] == "invalid_grant"
    assert "CANARY" not in str(error) and "CANARY" not in json.dumps(error.details)


@pytest.mark.asyncio
async def test_a_description_without_a_registered_code_is_not_shown():
    error = await _refused({"error": "something_else", "error_description": "Free server text."})
    assert "server_reason" not in error.details and "Free server text." not in str(error)

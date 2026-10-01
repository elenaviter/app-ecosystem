"""W408: the client stores a refresh's attempt id before sending, and retries with it.

A refresh whose response is lost after the server rotated leaves the stored
token consumed. The same attempt id sent again lets the server recognise the
retry. These tests drive the real profile session with the package's fake
stores; the server side is covered by the Connection Hub PostgreSQL tests.
"""

from __future__ import annotations

import pytest

from connection_hub.caller.errors import AuthorizationError

from test_oauth_profiles import _OAuth, _profile, _service, _token


def _lost_response() -> AuthorizationError:
    error = AuthorizationError("oauth_token_request_failed", "OAuth token POST could not be reached (ReadError).")
    error.status = None
    error.details = {"failure_kind": "ReadError"}
    return error


def _refused() -> AuthorizationError:
    # The token endpoint's own refusal: a 4xx with a registered OAuth error
    # code, which the transport records as details["oauth_error"].
    error = AuthorizationError("oauth_token_request_failed", "The OAuth server rejected refresh.")
    error.status = 400
    error.details = {"status": 400, "oauth_error": "invalid_grant"}
    return error


def _answered_without_a_grant_decision(status: int) -> AuthorizationError:
    # A gateway, proxy or tunnel status, or a 4xx with no OAuth error body.
    error = AuthorizationError(
        "oauth_token_request_failed", f"OAuth token POST returned HTTP {status}."
    )
    error.status = status
    error.details = {"status": status}
    return error


class _RecordingOAuth(_OAuth):
    """Records what the credential store held at the moment each refresh was sent."""

    def __init__(self, credentials_ref, credentials, **kwargs) -> None:
        super().__init__(**kwargs)
        self._credentials = credentials
        self._ref = credentials_ref
        self.stored_at_send: list[str] = []

    async def refresh(self, **kwargs):
        stored = self._credentials.values.get(self._ref())
        self.stored_at_send.append(stored.refresh_attempt if stored else "")
        return await super().refresh(**kwargs)


def _setup(tmp_path):
    holder = {}
    service, profiles, credentials = _service(tmp_path)
    oauth = _RecordingOAuth(lambda: holder["ref"], credentials)
    service._oauth = oauth  # noqa: SLF001 - the fake records what was stored at send time
    profile = _profile()
    holder["ref"] = profile.credential_ref
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=10_000_000_000))
    return service, profile, credentials, oauth


@pytest.mark.asyncio
async def test_the_attempt_id_is_stored_before_the_request_and_sent_with_it(tmp_path) -> None:
    service, profile, credentials, oauth = _setup(tmp_path)
    await service.refresh_access_token(profile.name)
    sent = oauth.refresh_kwargs[0]["refresh_attempt"]
    assert len(sent) >= 16
    assert oauth.stored_at_send == [sent]
    # A successful refresh stores the new token without an attempt id.
    assert credentials.values[profile.credential_ref].refresh_attempt == ""
    assert credentials.values[profile.credential_ref].access_token == "refreshed-access"


@pytest.mark.asyncio
async def test_a_lost_response_keeps_the_attempt_and_the_retry_sends_the_same_one(tmp_path) -> None:
    service, profile, credentials, oauth = _setup(tmp_path)
    oauth.refresh_error = _lost_response()
    with pytest.raises(AuthorizationError):
        await service.refresh_access_token(profile.name)
    first = oauth.refresh_kwargs[0]["refresh_attempt"]
    assert credentials.values[profile.credential_ref].refresh_attempt == first
    # A second transport failure keeps the same attempt too.
    with pytest.raises(AuthorizationError):
        await service.refresh_access_token(profile.name)
    assert oauth.refresh_kwargs[1]["refresh_attempt"] == first
    # The retry that gets through sends that attempt, and success clears it.
    oauth.refresh_error = None
    assert await service.refresh_access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_kwargs[2]["refresh_attempt"] == first
    assert credentials.values[profile.credential_ref].refresh_attempt == ""


@pytest.mark.asyncio
async def test_a_definitive_refusal_clears_the_attempt(tmp_path) -> None:
    service, profile, credentials, oauth = _setup(tmp_path)
    oauth.refresh_error = _refused()
    with pytest.raises(AuthorizationError):
        await service.refresh_access_token(profile.name)
    stored = credentials.values[profile.credential_ref]
    assert stored.refresh_attempt == ""
    assert stored.refresh_token == "refresh-secret"


@pytest.mark.asyncio
async def test_a_store_that_cannot_take_the_attempt_sends_nothing(tmp_path) -> None:
    """W408 review: a refresh sent without a stored id could not be retried if lost."""

    service, profile, credentials, oauth = _setup(tmp_path)
    original = credentials.values[profile.credential_ref]
    original_put = credentials.put

    def failing_put(ref, token):
        raise OSError("keychain unavailable")

    credentials.put = failing_put
    with pytest.raises(AuthorizationError) as raised:
        await service.refresh_access_token(profile.name)
    assert raised.value.code == "oauth_refresh_attempt_unstored"
    assert oauth.refresh_calls == 0
    assert credentials.values[profile.credential_ref] == original
    # Once the store works again, the refresh goes through with an attempt id.
    credentials.put = original_put
    assert await service.refresh_access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_kwargs[0]["refresh_attempt"]


@pytest.mark.asyncio
async def test_the_attempt_id_never_appears_in_repr_or_errors(tmp_path) -> None:
    service, profile, credentials, oauth = _setup(tmp_path)
    oauth.refresh_error = _lost_response()
    with pytest.raises(AuthorizationError) as raised:
        await service.refresh_access_token(profile.name)
    attempt = credentials.values[profile.credential_ref].refresh_attempt
    assert attempt and attempt not in repr(credentials.values[profile.credential_ref])
    assert attempt not in str(raised.value)


def test_the_attempt_survives_the_native_store_round_trip_and_old_json_reads_as_none() -> None:
    from dataclasses import replace

    from connection_hub.caller.authorization.models import OAuthTokenSet

    attempt = "attempt-" + "c" * 40
    token = replace(_token(), refresh_attempt=attempt)
    assert OAuthTokenSet.from_secret_json(token.to_secret_json()).refresh_attempt == attempt
    assert OAuthTokenSet.from_secret_json(_token().to_secret_json()).refresh_attempt == ""
    # A malformed id is never written or read back.
    bad = replace(_token(), refresh_attempt="short")
    assert "refresh_attempt" not in bad.to_secret_json()


@pytest.mark.asyncio
async def test_the_client_sends_the_attempt_only_when_it_is_well_formed() -> None:
    from connection_hub.caller.authorization.client import OAuthClient
    from connection_hub.caller.authorization.models import (
        AuthorizationServerMetadata,
        OAuthClientRegistration,
    )

    class Capture:
        def __init__(self):
            self.forms = []

        async def post_form(self, url, payload):
            self.forms.append(dict(payload))
            return {"access_token": "a" * 20, "token_type": "Bearer", "expires_in": 60, "refresh_token": "r" * 20}

    transport = Capture()
    client = OAuthClient(transport=transport)
    metadata = AuthorizationServerMetadata(
        issuer="https://hub.example.test", authorization_endpoint="https://hub.example.test/a",
        token_endpoint="https://hub.example.test/t", registration_endpoint=None, revocation_endpoint=None,
        scopes_supported=(), supports_refresh=True, authorization_response_issuer_required=False,
    )
    registration = OAuthClientRegistration(client_id="client-w408", redirect_uris=("http://127.0.0.1/cb",))
    await client.refresh(metadata=metadata, client=registration, resource=None, refresh_token="r" * 20,
                         refresh_attempt="attempt-" + "d" * 40)
    await client.refresh(metadata=metadata, client=registration, resource=None, refresh_token="r" * 20)
    await client.refresh(metadata=metadata, client=registration, resource=None, refresh_token="r" * 20,
                         refresh_attempt="bad id!")
    assert transport.forms[0]["refresh_attempt"] == "attempt-" + "d" * 40
    assert "refresh_attempt" not in transport.forms[1]
    assert "refresh_attempt" not in transport.forms[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504, 408, 429, 400])
async def test_an_answer_that_is_not_the_endpoint_s_refusal_keeps_the_attempt(
    tmp_path, status
) -> None:
    """W408 review: a proxy's 5xx after the endpoint rotated must not drop the
    attempt, or the next refresh sends a new one and is judged reuse."""

    service, profile, credentials, oauth = _setup(tmp_path)
    oauth.refresh_error = _answered_without_a_grant_decision(status)
    with pytest.raises(AuthorizationError):
        await service.refresh_access_token(profile.name)
    first = oauth.refresh_kwargs[0]["refresh_attempt"]
    assert credentials.values[profile.credential_ref].refresh_attempt == first
    oauth.refresh_error = None
    assert await service.refresh_access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_kwargs[1]["refresh_attempt"] == first, "the retry carries the same attempt"

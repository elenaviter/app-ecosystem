# SPDX-License-Identifier: MIT
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from connection_hub.browser_session.model import LoginAttempt
from connection_hub.browser_session.oidc import OidcClientConfig, OidcCodeFlow, OidcEndpoints, pkce_challenge
from connection_hub.browser_session.protocols import UpstreamRejected

ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_pool"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": "https://auth.example.test/oauth2/authorize",
    "token_endpoint": "https://auth.example.test/oauth2/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
}


class FakeVerifier:
    def __init__(self, claims):
        self.claims = claims
        self.calls = []

    async def verify(self, id_token, *, audience, issuer=""):
        self.calls.append((id_token, audience, issuer))
        if id_token == "bad":
            raise UpstreamRejected("token_invalid", "signature")
        return dict(self.claims)


def _attempt(**over):
    base = dict(state="st", binding="b", nonce="n1", code_verifier="v" * 43, next_path="/", created_at=0, expires_at=999)
    base.update(over)
    return LoginAttempt(**base)


def _flow(claims=None, *, secret="", exchange=None, discovered=None):
    config = OidcClientConfig.cognito(
        issuer=ISSUER, client_id="cid", client_secret=secret,
        redirect_uri="https://app.test/api/platform/session/callback", hosted_ui_domain="https://auth.example.test",
    )
    calls = {"discover": 0, "exchange": []}

    async def discover(issuer):
        calls["discover"] += 1
        assert issuer == ISSUER
        return dict(discovered or DISCOVERY)

    async def default_exchange(endpoint, form, headers):
        calls["exchange"].append((endpoint, dict(form), dict(headers)))
        return {"id_token": "good", "access_token": "a"}

    verifier = FakeVerifier(claims or {"sub": "u1", "nonce": "n1", "email": "p@example.test", "email_verified": "true", "cognito:username": "person"})
    flow = OidcCodeFlow(config, verifier=verifier, exchange=exchange or default_exchange, discover=discover)
    return flow, verifier, calls


async def test_begin_builds_the_authorize_url_with_state_nonce_and_pkce():
    flow, _, calls = _flow()
    url = await flow.begin(_attempt())
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == DISCOVERY["authorization_endpoint"]
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query["response_type"] == "code" and query["client_id"] == "cid"
    assert query["redirect_uri"] == "https://app.test/api/platform/session/callback"
    assert query["scope"] == "openid email profile"
    assert query["state"] == "st" and query["nonce"] == "n1"
    assert query["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(("v" * 43).encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == expected == pkce_challenge("v" * 43)
    await flow.begin(_attempt())
    assert calls["discover"] == 1, "discovery is fetched once"


async def test_complete_exchanges_the_code_with_pkce_and_binds_the_nonce():
    flow, verifier, calls = _flow(secret="shh")
    identity = await flow.complete({"state": "st", "code": "abc"}, _attempt())
    endpoint, form, headers = calls["exchange"][0]
    assert endpoint == DISCOVERY["token_endpoint"]
    assert form["grant_type"] == "authorization_code" and form["code"] == "abc"
    assert form["code_verifier"] == "v" * 43 and form["client_id"] == "cid"
    assert headers["Authorization"].startswith("Basic ")
    assert "client_secret" not in form
    assert verifier.calls == [("good", "cid", ISSUER)]
    assert identity.provider == "cognito" and identity.subject == "u1"
    assert identity.email == "p@example.test" and identity.email_verified is True
    assert identity.name == "person"
    assert identity.canonical_subject == "cognito:u1"


@pytest.mark.parametrize(
    "params, attempt, claims, reason",
    [
        ({"error": "access_denied", "error_description": "no"}, {}, None, "access_denied"),
        ({"state": "other", "code": "c"}, {}, None, "state_mismatch"),
        ({"state": "st"}, {}, None, "code_missing"),
        ({"state": "st", "code": "c"}, {}, {"sub": "u1", "nonce": "wrong"}, "nonce_mismatch"),
        ({"state": "st", "code": "c"}, {}, {"nonce": "n1"}, "subject_missing"),
    ],
)
async def test_complete_refuses_every_broken_callback(params, attempt, claims, reason):
    flow, _, _ = _flow(claims)
    with pytest.raises(UpstreamRejected) as rejected:
        await flow.complete(params, _attempt(**attempt))
    assert rejected.value.reason == reason


async def test_token_endpoint_without_id_token_and_bad_signature_are_refused():
    async def no_id_token(endpoint, form, headers):
        return {"access_token": "a"}

    flow, _, _ = _flow(exchange=no_id_token)
    with pytest.raises(UpstreamRejected) as rejected:
        await flow.complete({"state": "st", "code": "c"}, _attempt())
    assert rejected.value.reason == "id_token_missing"

    async def bad(endpoint, form, headers):
        return {"id_token": "bad"}

    flow, _, _ = _flow(exchange=bad)
    with pytest.raises(UpstreamRejected) as rejected:
        await flow.complete({"state": "st", "code": "c"}, _attempt())
    assert rejected.value.reason == "token_invalid"


async def test_cognito_logout_url_and_discovery_gaps():
    flow, _, _ = _flow()
    assert flow.logout_url(post_logout_redirect="https://app.test/") == (
        "https://auth.example.test/logout?client_id=cid&logout_uri=https%3A%2F%2Fapp.test%2F"
    )
    generic = OidcCodeFlow(
        OidcClientConfig(issuer=ISSUER, client_id="cid", redirect_uri="https://app.test/cb", endpoints=OidcEndpoints.from_discovery({**DISCOVERY, "end_session_endpoint": "https://auth.example.test/logout"})),
        verifier=FakeVerifier({}),
    )
    assert generic.logout_url(post_logout_redirect="https://app.test/") == (
        "https://auth.example.test/logout?client_id=cid&post_logout_redirect_uri=https%3A%2F%2Fapp.test%2F"
    )
    assert OidcCodeFlow(OidcClientConfig(issuer=ISSUER, client_id="cid", redirect_uri="https://app.test/cb"), verifier=FakeVerifier({})).logout_url() == ""
    with pytest.raises(UpstreamRejected):
        OidcEndpoints.from_discovery({"issuer": ISSUER})
    with pytest.raises(ValueError):
        OidcClientConfig(issuer="", client_id="cid", redirect_uri="x")

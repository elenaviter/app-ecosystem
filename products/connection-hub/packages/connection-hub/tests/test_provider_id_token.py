# SPDX-License-Identifier: MIT

"""A provider ID token names a member only after it is verified.

Tokens are signed with a real RSA key and checked against issuer metadata the
test serves, so every refusal below is the verifier's own decision, not a
payload that merely failed to decode.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from connection_hub.delegated_to_kdcube.provider_id_token import (
    ProviderIdTokenVerifier,
    ProviderIdentityUnverified,
)

DISCOVERY_URL = "https://issuer.example/.well-known/openid-configuration"
ISSUER = "https://issuer.example/oauth"
JWKS_URI = "https://issuer.example/oauth/jwks"
CLIENT_ID = "client-app-1"
KID = "key-1"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _rsa_key()
OTHER_KEY = _rsa_key()


class _Jwks:
    """PyJWKClient's contract: the signing key of a token's kid, or PyJWKClientError."""

    def __init__(self, public_keys: dict):
        self.keys = public_keys

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return jwt.PyJWK(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid])))


def _discovery(**overrides):
    document = {
        "issuer": ISSUER,
        "jwks_uri": JWKS_URI,
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    document.update(overrides)
    return document


def _verifier(document=None, *, clock=time.time, fetches=None):
    async def fetch_json(url):
        if fetches is not None:
            fetches.append(url)
        return document if document is not None else _discovery()

    return ProviderIdTokenVerifier(
        DISCOVERY_URL,
        fetch_json=fetch_json,
        jwks_client_factory=lambda uri: _Jwks({KID: SIGNING_KEY.public_key()}),
        clock=clock,
    )


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "dE5aOhH-ap",
        "iat": now,
        "exp": now + 600,
        "email": "jane@example.com",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _token(key=SIGNING_KEY, *, kid=KID, **claims):
    return jwt.encode(_claims(**claims), key, algorithm="RS256", headers={"kid": kid})


async def _refused(token, **kwargs) -> str:
    with pytest.raises(ProviderIdentityUnverified) as refused:
        await _verifier(kwargs.pop("document", None)).verify(token, audience=kwargs.pop("audience", CLIENT_ID), **kwargs)
    return refused.value.reason


async def test_a_valid_token_names_its_member():
    claims = await _verifier().verify(_token(), audience=CLIENT_ID)
    assert claims["sub"] == "dE5aOhH-ap"
    assert claims["email"] == "jane@example.com"


async def test_a_signature_from_another_key_is_refused():
    assert await _refused(_token(OTHER_KEY)) == "signature_invalid"


async def test_a_token_of_another_issuer_is_refused():
    assert await _refused(_token(iss="https://www.linkedin.com")) == "issuer_mismatch"


async def test_a_token_for_another_client_is_refused():
    assert await _refused(_token(aud="someone-elses-app")) == "audience_mismatch"


async def test_an_expired_token_is_refused():
    now = int(time.time())
    assert await _refused(_token(iat=now - 7200, exp=now - 3600)) == "expired"


async def test_a_token_issued_in_the_future_is_refused():
    now = int(time.time())
    assert await _refused(_token(iat=now + 3600, exp=now + 7200)) == "not_yet_valid"


async def test_a_symmetric_algorithm_is_refused_before_any_key_is_looked_up():
    token = jwt.encode(_claims(), "client-secret", algorithm="HS256", headers={"kid": KID})
    assert await _refused(token) == "algorithm_not_allowed"


async def test_an_unsigned_token_is_refused():
    header = jwt.utils.base64url_encode(json.dumps({"alg": "none", "kid": KID}).encode()).decode()
    payload = jwt.utils.base64url_encode(json.dumps(_claims()).encode()).decode()
    assert await _refused(f"{header}.{payload}.") == "algorithm_not_allowed"


async def test_a_payload_only_token_is_refused():
    payload = jwt.utils.base64url_encode(json.dumps(_claims()).encode()).decode()
    assert await _refused(f"header.{payload}.signature") == "id_token_malformed"


async def test_a_malformed_token_is_refused():
    assert await _refused("not-a-jwt") == "id_token_malformed"


async def test_a_key_id_the_issuer_does_not_publish_is_refused():
    assert await _refused(_token(kid="rotated-away")) == "signing_key_unknown"


async def test_a_token_without_a_subject_is_refused():
    assert await _refused(_token(sub=None)) == "claim_missing"


async def test_a_missing_token_is_refused():
    assert await _refused("") == "id_token_missing"


async def test_without_the_client_id_no_token_can_be_accepted():
    assert await _refused(_token(), audience="") == "audience_unknown"


async def test_a_nonce_sent_with_the_request_must_come_back():
    assert await _refused(_token(nonce="other"), nonce="sent") == "nonce_mismatch"
    claims = await _verifier().verify(_token(nonce="sent"), audience=CLIENT_ID, nonce="sent")
    assert claims["nonce"] == "sent"


async def test_an_issuer_declaring_only_symmetric_algorithms_is_refused():
    document = _discovery(id_token_signing_alg_values_supported=["HS256"])
    assert await _refused(_token(), document=document) == "algorithm_not_allowed"


async def test_incomplete_issuer_metadata_is_refused():
    document = _discovery(jwks_uri="")
    assert await _refused(_token(), document=document) == "issuer_metadata_incomplete"


async def test_issuer_metadata_is_cached_and_refreshed_after_its_lifetime():
    now = [1_000_000.0]
    fetches: list[str] = []
    verifier = _verifier(clock=lambda: now[0], fetches=fetches)

    await verifier.verify(_token(), audience=CLIENT_ID)
    await verifier.verify(_token(), audience=CLIENT_ID)
    assert fetches == [DISCOVERY_URL]

    now[0] += 3600
    await verifier.verify(_token(), audience=CLIENT_ID)
    assert fetches == [DISCOVERY_URL, DISCOVERY_URL]


async def test_the_issuer_is_taken_from_metadata_not_assumed():
    document = _discovery(issuer="https://www.linkedin.com")
    claims = await _verifier(document).verify(_token(iss="https://www.linkedin.com"), audience=CLIENT_ID)
    assert claims["iss"] == "https://www.linkedin.com"

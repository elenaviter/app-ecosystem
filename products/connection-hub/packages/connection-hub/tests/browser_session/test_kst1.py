# SPDX-License-Identifier: MIT
import base64
import hashlib
import hmac
import json

import pytest

from connection_hub.browser_session import kst1


def test_round_trip_and_wire_shape():
    claims = {"schema": kst1.CLAIMS_SCHEMA, "sid": "s1", "sub": "google:1", "ver": 1, "iat": 1, "exp": 2}
    token = kst1.encode(claims, secret="secret")
    prefix, body, signature = token.split(".")
    assert prefix == "kst1"
    # The body is compact JSON with sorted keys, base64url without padding.
    padded = body + "=" * (-len(body) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == claims
    assert base64.urlsafe_b64decode(padded) == json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    assert "=" not in body and "=" not in signature
    expected = hmac.new(b"secret", body.encode("ascii"), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)) == expected
    assert kst1.decode(token, secret="secret") == claims


@pytest.mark.parametrize(
    "broken",
    ["", "kst1", "kst1.a", "kst2.a.b", "kst1..b", "kst1.a.", "kst1.notjson.sig", "kst1.eyJhIjoxfQ.bad"],
)
def test_malformed_or_forged_tokens_are_refused(broken):
    with pytest.raises(kst1.TokenInvalid):
        kst1.decode(broken, secret="secret")


def test_wrong_secret_is_refused_and_hash_is_stable():
    token = kst1.encode({"sid": "s"}, secret="a")
    with pytest.raises(kst1.TokenInvalid):
        kst1.decode(token, secret="b")
    assert kst1.token_hash(token) == hashlib.sha256(token.encode()).hexdigest()
    with pytest.raises(kst1.TokenInvalid):
        kst1.encode({"sid": "s"}, secret="")

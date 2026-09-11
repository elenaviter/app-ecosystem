# SPDX-License-Identifier: MIT
import pytest

from connection_hub.server_side_login.google_identity import GoogleIdentityUpstream
from connection_hub.server_side_login.model import LoginAttempt
from connection_hub.server_side_login.protocols import UpstreamRejected


class FakeVerifier:
    def __init__(self, claims):
        self.claims = claims
        self.calls = []

    async def verify(self, id_token, *, audience, issuer=""):
        self.calls.append((id_token, audience, issuer))
        return dict(self.claims)


def _attempt(nonce="n1"):
    return LoginAttempt(state="s", binding="b", nonce=nonce, code_verifier="v", next_path="/", created_at=0, expires_at=9)


async def test_google_credential_is_verified_against_the_client_id_and_bound_by_nonce():
    verifier = FakeVerifier({"iss": "https://accounts.google.com", "sub": "g1", "email": "p@example.test", "email_verified": True, "nonce": "n1", "name": "P"})
    upstream = GoogleIdentityUpstream(client_id="web-client", verifier=verifier)
    assert upstream.name == "google" and await upstream.begin(_attempt()) == ""
    identity = await upstream.complete({"credential": "jwt"}, _attempt())
    assert verifier.calls == [("jwt", "web-client", "")]
    assert identity.canonical_subject == "google:g1" and identity.email_verified is True
    assert upstream.logout_url() == ""


@pytest.mark.parametrize(
    "claims, params, reason",
    [
        ({"iss": "https://accounts.google.com", "sub": "g1", "nonce": "n1"}, {}, "credential_missing"),
        ({"iss": "https://evil.test", "sub": "g1", "nonce": "n1"}, {"credential": "j"}, "issuer_mismatch"),
        ({"iss": "accounts.google.com", "sub": "g1", "nonce": "other"}, {"credential": "j"}, "nonce_mismatch"),
        ({"iss": "accounts.google.com", "sub": "g1"}, {"credential": "j"}, "nonce_mismatch"),
        ({"iss": "accounts.google.com", "nonce": "n1"}, {"credential": "j"}, "subject_missing"),
    ],
)
async def test_google_refusals(claims, params, reason):
    upstream = GoogleIdentityUpstream(client_id="web-client", verifier=FakeVerifier(claims))
    with pytest.raises(UpstreamRejected) as rejected:
        await upstream.complete(params, _attempt())
    assert rejected.value.reason == reason


async def test_nonce_optional_when_the_page_did_not_send_one():
    upstream = GoogleIdentityUpstream(client_id="c", verifier=FakeVerifier({"iss": "accounts.google.com", "sub": "g"}), require_nonce=False)
    assert (await upstream.complete({"credential": "j"}, _attempt())).subject == "g"

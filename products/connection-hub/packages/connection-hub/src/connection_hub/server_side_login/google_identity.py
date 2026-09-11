# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The Google Identity Services authenticator.

Google Identity Services signs the person in inside the host's login page and
posts a signed ID token (the "credential") to the host. There is no redirect
to begin, so ``begin`` returns an empty string: the host renders its login
page with the attempt's ``nonce`` and ``state`` and posts them back together
with the credential. ``complete`` verifies the credential through the injected
verifier against Google's issuers and the configured client id, and binds it
to the attempt through the nonce when the page sent one.

This is the port of the platform's reference Google login, with the three
hardening gaps that reference lists closed by construction: the attempt is
one-time and bound to the browser, the nonce reaches Google and is checked,
and the destination goes through the same-origin guard in the flow.
"""

from __future__ import annotations

import hmac
from typing import Any, Mapping

from connection_hub.server_side_login.model import LoginAttempt, VerifiedIdentity
from connection_hub.server_side_login.protocols import IdTokenVerifier, UpstreamRejected

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


class GoogleIdentityUpstream:
    """``UpstreamIdentity`` for a posted Google Identity Services credential."""

    def __init__(self, *, client_id: str, verifier: IdTokenVerifier, require_nonce: bool = True) -> None:
        if not str(client_id or "").strip():
            raise ValueError("google identity needs the web client id")
        self._client_id = client_id.strip()
        self._verifier = verifier
        self._require_nonce = bool(require_nonce)

    @property
    def name(self) -> str:
        return "google"

    @property
    def client_id(self) -> str:
        return self._client_id

    async def begin(self, attempt: LoginAttempt) -> str:
        return ""

    async def complete(self, params: Mapping[str, Any], attempt: LoginAttempt) -> VerifiedIdentity:
        credential = str(params.get("credential") or "").strip()
        if not credential:
            raise UpstreamRejected("credential_missing", "no Google credential was posted")
        claims = await self._verifier.verify(credential, audience=self._client_id, issuer="")
        issuer = str(claims.get("iss") or "")
        if issuer not in GOOGLE_ISSUERS:
            raise UpstreamRejected("issuer_mismatch", "the credential was not issued by Google")
        nonce = str(claims.get("nonce") or "")
        if nonce or self._require_nonce:
            if not nonce or not hmac.compare_digest(nonce.encode("utf-8"), attempt.nonce.encode("utf-8")):
                raise UpstreamRejected("nonce_mismatch", "the credential was not issued for this attempt")
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise UpstreamRejected("subject_missing", "the credential carries no subject")
        return VerifiedIdentity(
            provider="google",
            subject=subject,
            email=str(claims.get("email") or "").strip(),
            email_verified=bool(claims.get("email_verified")),
            name=str(claims.get("name") or "").strip(),
            claims=dict(claims),
        )

    def logout_url(self, *, post_logout_redirect: str = "") -> str:
        return ""


__all__ = ["GOOGLE_ISSUERS", "GOOGLE_JWKS_URL", "GoogleIdentityUpstream"]

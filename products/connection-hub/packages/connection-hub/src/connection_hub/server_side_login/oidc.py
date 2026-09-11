# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The OIDC authorization-code authenticator: Cognito or any OIDC issuer.

The browser is sent to the issuer's authorization endpoint with ``state``,
``nonce``, and a PKCE S256 challenge, all from the one-time login attempt.
The callback brings ``code`` and ``state`` back; the server exchanges the code
at the token endpoint (a confidential client keeps its secret here, never in
the browser), verifies the ID token through the injected verifier (signature,
issuer, audience, expiry), and checks the ``nonce`` claim against the attempt.
Only then is there an identity.

Network calls are injectable so tests run against fakes: ``exchange`` posts
the code, ``discover`` fetches the issuer's discovery document. The defaults
use ``httpx``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode

from connection_hub.server_side_login.model import LoginAttempt, VerifiedIdentity
from connection_hub.server_side_login.protocols import IdTokenVerifier, UpstreamRejected

DISCOVERY_PATH = "/.well-known/openid-configuration"
DEFAULT_SCOPES = ("openid", "email", "profile")

TokenExchanger = Callable[[str, Mapping[str, str], Mapping[str, str]], Awaitable[Mapping[str, Any]]]
Discoverer = Callable[[str], Awaitable[Mapping[str, Any]]]


def pkce_challenge(code_verifier: str) -> str:
    """The S256 challenge of a PKCE verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class OidcEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str = ""
    end_session_endpoint: str = ""
    issuer: str = ""

    @classmethod
    def from_discovery(cls, document: Mapping[str, Any]) -> "OidcEndpoints":
        authorization = str(document.get("authorization_endpoint") or "").strip()
        token = str(document.get("token_endpoint") or "").strip()
        if not authorization or not token:
            raise UpstreamRejected("discovery_incomplete", "issuer discovery lacks the authorization or token endpoint")
        return cls(
            authorization_endpoint=authorization,
            token_endpoint=token,
            jwks_uri=str(document.get("jwks_uri") or "").strip(),
            end_session_endpoint=str(document.get("end_session_endpoint") or "").strip(),
            issuer=str(document.get("issuer") or "").strip(),
        )


@dataclass(frozen=True)
class OidcClientConfig:
    """The relying-party configuration. ``endpoints`` skips discovery when
    given. ``logout_redirect_param`` is ``post_logout_redirect_uri`` per the
    OIDC RP-initiated logout; Cognito's ``/logout`` wants ``logout_uri``."""

    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str = ""
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    provider: str = "oidc"
    endpoints: OidcEndpoints | None = None
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    logout_redirect_param: str = "post_logout_redirect_uri"
    end_session_endpoint: str = ""

    def __post_init__(self) -> None:
        for name in ("issuer", "client_id", "redirect_uri"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"oidc client config needs {name}")

    @classmethod
    def cognito(
        cls,
        *,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str = "",
        hosted_ui_domain: str = "",
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
    ) -> "OidcClientConfig":
        """A Cognito user pool. ``hosted_ui_domain`` (``https://<domain>``)
        names the hosted UI whose ``/logout`` ends the upstream session; the
        pool's discovery document does not advertise it."""
        domain = str(hosted_ui_domain or "").rstrip("/")
        return cls(
            issuer=issuer,
            client_id=client_id,
            redirect_uri=redirect_uri,
            client_secret=client_secret,
            scopes=scopes,
            provider="cognito",
            logout_redirect_param="logout_uri",
            end_session_endpoint=f"{domain}/logout" if domain else "",
        )


async def _default_exchange(token_endpoint: str, form: Mapping[str, str], headers: Mapping[str, str]) -> Mapping[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(token_endpoint, data=dict(form), headers=dict(headers))
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamRejected("token_exchange_failed", f"token endpoint answered {response.status_code} without JSON") from exc
    if response.status_code >= 400:
        error = str(payload.get("error") or "token_exchange_failed") if isinstance(payload, Mapping) else "token_exchange_failed"
        raise UpstreamRejected(error, f"token endpoint answered {response.status_code}")
    if not isinstance(payload, Mapping):
        raise UpstreamRejected("token_exchange_failed", "token endpoint answered a non-object")
    return payload


async def _default_discover(issuer: str) -> Mapping[str, Any]:
    import httpx

    url = issuer.rstrip("/") + DISCOVERY_PATH
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url)
    if response.status_code != 200:
        raise UpstreamRejected("discovery_failed", f"issuer discovery answered {response.status_code}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise UpstreamRejected("discovery_failed", "issuer discovery answered a non-object")
    return payload


class OidcCodeFlow:
    """``UpstreamIdentity`` over the OIDC authorization-code flow with PKCE."""

    def __init__(
        self,
        config: OidcClientConfig,
        *,
        verifier: IdTokenVerifier,
        exchange: TokenExchanger | None = None,
        discover: Discoverer | None = None,
    ) -> None:
        self._config = config
        self._verifier = verifier
        self._exchange = exchange or _default_exchange
        self._discover = discover or _default_discover
        self._endpoints: OidcEndpoints | None = config.endpoints

    @property
    def name(self) -> str:
        return self._config.provider

    @property
    def config(self) -> OidcClientConfig:
        return self._config

    async def endpoints(self) -> OidcEndpoints:
        if self._endpoints is None:
            self._endpoints = OidcEndpoints.from_discovery(await self._discover(self._config.issuer))
        return self._endpoints

    async def begin(self, attempt: LoginAttempt) -> str:
        endpoints = await self.endpoints()
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": pkce_challenge(attempt.code_verifier),
            "code_challenge_method": "S256",
        }
        for key, value in dict(self._config.extra_authorize_params).items():
            params.setdefault(str(key), str(value))
        separator = "&" if "?" in endpoints.authorization_endpoint else "?"
        return f"{endpoints.authorization_endpoint}{separator}{urlencode(params)}"

    async def complete(self, params: Mapping[str, Any], attempt: LoginAttempt) -> VerifiedIdentity:
        error = str(params.get("error") or "").strip()
        if error:
            raise UpstreamRejected(error, str(params.get("error_description") or "").strip())
        state = str(params.get("state") or "")
        if not state or not hmac.compare_digest(state.encode("utf-8"), attempt.state.encode("utf-8")):
            raise UpstreamRejected("state_mismatch", "the callback state is not the started attempt")
        code = str(params.get("code") or "").strip()
        if not code:
            raise UpstreamRejected("code_missing", "the callback carries no authorization code")
        endpoints = await self.endpoints()
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": self._config.client_id,
            "code_verifier": attempt.code_verifier,
        }
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._config.client_secret:
            basic = base64.b64encode(
                f"{self._config.client_id}:{self._config.client_secret}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        tokens = await self._exchange(endpoints.token_endpoint, form, headers)
        id_token = str(tokens.get("id_token") or "").strip()
        if not id_token:
            raise UpstreamRejected("id_token_missing", "the token endpoint returned no ID token")
        claims = await self._verifier.verify(
            id_token,
            audience=self._config.client_id,
            issuer=endpoints.issuer or self._config.issuer,
        )
        nonce = str(claims.get("nonce") or "")
        if not nonce or not hmac.compare_digest(nonce.encode("utf-8"), attempt.nonce.encode("utf-8")):
            raise UpstreamRejected("nonce_mismatch", "the ID token was not issued for this attempt")
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise UpstreamRejected("subject_missing", "the ID token carries no subject")
        email_verified = claims.get("email_verified")
        if isinstance(email_verified, str):
            email_verified = email_verified.strip().lower() == "true"
        return VerifiedIdentity(
            provider=self._config.provider,
            subject=subject,
            email=str(claims.get("email") or "").strip(),
            email_verified=bool(email_verified),
            name=str(
                claims.get("name")
                or claims.get("preferred_username")
                or claims.get("cognito:username")
                or ""
            ).strip(),
            claims=dict(claims),
        )

    def logout_url(self, *, post_logout_redirect: str = "") -> str:
        endpoint = self._config.end_session_endpoint or (
            self._endpoints.end_session_endpoint if self._endpoints is not None else ""
        )
        if not endpoint:
            return ""
        params = {"client_id": self._config.client_id}
        if post_logout_redirect:
            params[self._config.logout_redirect_param] = post_logout_redirect
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(params)}"


__all__ = [
    "DEFAULT_SCOPES",
    "DISCOVERY_PATH",
    "Discoverer",
    "OidcClientConfig",
    "OidcCodeFlow",
    "OidcEndpoints",
    "TokenExchanger",
    "pkce_challenge",
]

# SPDX-License-Identifier: MIT

"""Provider ID tokens verified before their claims name a connected account.

A provider's ID token is a claim about who the member is, and it is worth only
its signature. Before any claim becomes a connected account's
``external_subject``, the token is checked against the issuer's published
metadata: signature by a key from the issuer's JWKS, an algorithm the issuer
declares and this module allows, ``iss``, ``aud`` against the connector app's
client id, ``exp``/``iat``, and ``nonce`` when the authorization request sent
one.

Issuer and JWKS location are read from the issuer's discovery document at run
time, cached for a bounded time, so a provider changing either does not need a
release. Keys come from PyJWT's ``PyJWKClient``, which caches them and refetches
the key set when a token names an unknown key id. PyJWT is an optional
dependency of this package; without it every token is refused.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx

# Asymmetric algorithms only. A symmetric one would let anyone holding the
# client secret mint a token, and ``none`` carries no signature at all.
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "ES256", "ES384"})
DEFAULT_METADATA_TTL_SECONDS = 3600
DEFAULT_LEEWAY_SECONDS = 60

FetchJson = Callable[[str], Awaitable[Mapping[str, Any]]]
JwksClientFactory = Callable[[str], Any]


class ProviderIdentityUnverified(RuntimeError):
    """A provider's identity evidence could not be verified.

    ``reason`` is a stable machine-readable cause; the message is for logs and
    operators.
    """

    code = "provider_identity_unverified"

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = str(reason or "id_token_invalid")
        super().__init__(message or self.reason)


@dataclass(frozen=True)
class IssuerMetadata:
    issuer: str
    jwks_uri: str
    algorithms: tuple[str, ...]


async def _fetch_json(url: str) -> Mapping[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ProviderIdentityUnverified(
            "issuer_metadata_unavailable", f"issuer discovery {url} failed: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise ProviderIdentityUnverified(
            "issuer_metadata_unavailable",
            f"issuer discovery {url} answered {response.status_code}",
        )
    try:
        body = response.json()
    except Exception as exc:
        raise ProviderIdentityUnverified(
            "issuer_metadata_unavailable", f"issuer discovery {url} answered non-JSON"
        ) from exc
    if not isinstance(body, Mapping):
        raise ProviderIdentityUnverified(
            "issuer_metadata_unavailable", f"issuer discovery {url} answered a non-object"
        )
    return body


def _pyjwt_jwks_client(jwks_uri: str) -> Any:
    from jwt import PyJWKClient

    return PyJWKClient(jwks_uri, cache_keys=True)


class ProviderIdTokenVerifier:
    """Verifies ID tokens of one issuer, named by its discovery document URL."""

    def __init__(
        self,
        discovery_url: str,
        *,
        metadata_ttl_seconds: int = DEFAULT_METADATA_TTL_SECONDS,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
        fetch_json: FetchJson | None = None,
        jwks_client_factory: JwksClientFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not str(discovery_url or "").strip():
            raise ValueError("ProviderIdTokenVerifier needs the issuer's discovery URL")
        self._discovery_url = str(discovery_url).strip()
        self._metadata_ttl = int(metadata_ttl_seconds)
        self._leeway = int(leeway_seconds)
        self._fetch_json = fetch_json or _fetch_json
        self._jwks_client_factory = jwks_client_factory or _pyjwt_jwks_client
        self._clock = clock
        self._metadata: IssuerMetadata | None = None
        self._metadata_at = 0.0
        self._jwks_clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def metadata(self) -> IssuerMetadata:
        async with self._lock:
            now = self._clock()
            if self._metadata is None or now - self._metadata_at >= self._metadata_ttl:
                self._metadata = self._parse_metadata(await self._fetch_json(self._discovery_url))
                self._metadata_at = now
            return self._metadata

    def _parse_metadata(self, document: Mapping[str, Any]) -> IssuerMetadata:
        issuer = str(document.get("issuer") or "").strip()
        jwks_uri = str(document.get("jwks_uri") or "").strip()
        if not issuer or not jwks_uri:
            raise ProviderIdentityUnverified(
                "issuer_metadata_incomplete",
                f"issuer discovery {self._discovery_url} lacks issuer or jwks_uri",
            )
        declared = document.get("id_token_signing_alg_values_supported") or ()
        algorithms = tuple(
            sorted({str(alg) for alg in declared if str(alg) in ALLOWED_ALGORITHMS})
        )
        if not algorithms:
            raise ProviderIdentityUnverified(
                "algorithm_not_allowed",
                f"issuer {issuer} declares no allowed ID token signing algorithm",
            )
        return IssuerMetadata(issuer=issuer, jwks_uri=jwks_uri, algorithms=algorithms)

    def _jwks_client(self, jwks_uri: str) -> Any:
        client = self._jwks_clients.get(jwks_uri)
        if client is None:
            client = self._jwks_client_factory(jwks_uri)
            self._jwks_clients[jwks_uri] = client
        return client

    async def verify(self, id_token: str, *, audience: str, nonce: str = "") -> dict[str, Any]:
        """The token's claims when it is valid; ``ProviderIdentityUnverified`` otherwise."""

        token = str(id_token or "").strip()
        if not token:
            raise ProviderIdentityUnverified("id_token_missing", "no ID token to verify")
        if not str(audience or "").strip():
            raise ProviderIdentityUnverified(
                "audience_unknown", "the connector app's client id is not known"
            )
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - depends on the host's environment
            raise ProviderIdentityUnverified(
                "verifier_unavailable", "ID token verification needs PyJWT with the crypto extra"
            ) from exc

        metadata = await self.metadata()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ProviderIdentityUnverified("id_token_malformed", str(exc)) from exc
        algorithm = str(header.get("alg") or "")
        if algorithm not in metadata.algorithms:
            raise ProviderIdentityUnverified(
                "algorithm_not_allowed",
                f"ID token algorithm {algorithm or '<none>'} is not one of {list(metadata.algorithms)}",
            )
        claims = await asyncio.to_thread(
            self._decode,
            token,
            jwks_uri=metadata.jwks_uri,
            algorithm=algorithm,
            audience=str(audience).strip(),
            issuer=metadata.issuer,
        )
        expected_nonce = str(nonce or "").strip()
        if expected_nonce and str(claims.get("nonce") or "") != expected_nonce:
            raise ProviderIdentityUnverified("nonce_mismatch", "ID token nonce does not match the request")
        return claims

    def _decode(
        self, token: str, *, jwks_uri: str, algorithm: str, audience: str, issuer: str
    ) -> dict[str, Any]:
        import jwt

        try:
            key = self._jwks_client(jwks_uri).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise ProviderIdentityUnverified("signing_key_unknown", str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise ProviderIdentityUnverified("id_token_malformed", str(exc)) from exc
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=audience,
                issuer=issuer,
                leeway=self._leeway,
                options={"require": ["iss", "aud", "sub", "exp", "iat"]},
            )
        except jwt.InvalidSignatureError as exc:
            raise ProviderIdentityUnverified("signature_invalid", str(exc)) from exc
        except jwt.ExpiredSignatureError as exc:
            raise ProviderIdentityUnverified("expired", str(exc)) from exc
        except jwt.InvalidIssuerError as exc:
            raise ProviderIdentityUnverified("issuer_mismatch", str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise ProviderIdentityUnverified("audience_mismatch", str(exc)) from exc
        except (jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError) as exc:
            raise ProviderIdentityUnverified("not_yet_valid", str(exc)) from exc
        except jwt.MissingRequiredClaimError as exc:
            raise ProviderIdentityUnverified("claim_missing", str(exc)) from exc
        except jwt.DecodeError as exc:
            raise ProviderIdentityUnverified("id_token_malformed", str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise ProviderIdentityUnverified("id_token_invalid", str(exc)) from exc
        if not str(claims.get("sub") or "").strip():
            raise ProviderIdentityUnverified("claim_missing", "ID token carries an empty sub")
        return dict(claims)


_VERIFIERS: dict[str, ProviderIdTokenVerifier] = {}


def provider_id_token_verifier(discovery_url: str) -> ProviderIdTokenVerifier:
    """The process-wide verifier of one issuer, so its metadata and keys stay cached."""

    key = str(discovery_url or "").strip()
    verifier = _VERIFIERS.get(key)
    if verifier is None:
        verifier = ProviderIdTokenVerifier(key)
        _VERIFIERS[key] = verifier
    return verifier


__all__ = [
    "ALLOWED_ALGORITHMS",
    "IssuerMetadata",
    "ProviderIdTokenVerifier",
    "ProviderIdentityUnverified",
    "provider_id_token_verifier",
]

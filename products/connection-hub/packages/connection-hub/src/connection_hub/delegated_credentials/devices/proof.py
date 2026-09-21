# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Request-bound proof creation and verification for profile devices.

Replay reservation is external: a caller verifies here, reserves the returned
``jti`` in bounded state, and then performs its consuming mutation.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from jwcrypto import jwk, jws

from connection_hub.delegated_credentials.devices._validation import (
    b64url,
    bounded_text,
    compact_json,
    json_object,
)
from connection_hub.delegated_credentials.devices.errors import DeviceCryptoError
from connection_hub.delegated_credentials.devices.keys import (
    jwk_thumbprint,
    parse_private_key,
    parse_public_jwk,
)

DEVICE_PROOF_ALGORITHM = "ES256"
DEVICE_PROOF_TYPE = "dpop+jwt"
DEFAULT_PROOF_WINDOW_SECONDS = 60
MAX_PROOF_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedDeviceProof:
    """Trusted proof facts after signature and request-binding validation."""

    thumbprint: str
    public_jwk: Mapping[str, str]
    jti: str
    issued_at: int
    method: str
    uri: str
    nonce: str


def normalize_proof_uri(value: str, *, claim: bool = False) -> str:
    """Canonical HTTP target URI used by DPoP ``htu`` comparisons."""

    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 8192:
        raise DeviceCryptoError("device_proof_uri_invalid")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise DeviceCryptoError("device_proof_uri_invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (claim and (parsed.query or parsed.fragment))
    ):
        raise DeviceCryptoError("device_proof_uri_invalid")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DeviceCryptoError("device_proof_uri_invalid") from exc
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    authority = hostname if port in (None, default_port) else f"{hostname}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            authority,
            parsed.path or "/",
            "",
            "",
        )
    )


def _request_method(value: Any) -> str:
    method = str(value or "").strip().upper()
    if (
        not method
        or len(method) > 32
        or not method.isascii()
        or not all(character.isalpha() or character == "-" for character in method)
    ):
        raise DeviceCryptoError("device_proof_method_invalid")
    return method


def _access_token_hash(access_token: str) -> str:
    token = bounded_text(
        access_token, reason="device_proof_access_token_invalid", maximum=65536
    )
    if not token.isascii():
        raise DeviceCryptoError("device_proof_access_token_invalid")
    return b64url(hashlib.sha256(token.encode("ascii")).digest())


def build_device_proof(
    private_jwk: Any,
    *,
    method: str,
    uri: str,
    nonce: str,
    access_token: str = "",
    now: int | None = None,
    jti: str | None = None,
) -> str:
    """Create one request-specific DPoP proof."""

    key = parse_private_key(private_jwk)
    public_value = parse_public_jwk(key.export(private_key=False))
    issued_at = int(time.time() if now is None else now)
    proof_id = bounded_text(
        jti or uuid.uuid4().hex, reason="device_proof_jti_invalid", maximum=128
    )
    nonce_value = bounded_text(
        nonce, reason="device_proof_nonce_invalid", maximum=1024
    )
    claims: dict[str, Any] = {
        "htm": _request_method(method),
        "htu": normalize_proof_uri(uri),
        "iat": issued_at,
        "jti": proof_id,
        "nonce": nonce_value,
    }
    if access_token:
        claims["ath"] = _access_token_hash(access_token)
    protected = {
        "alg": DEVICE_PROOF_ALGORITHM,
        "jwk": public_value,
        "typ": DEVICE_PROOF_TYPE,
    }
    token = jws.JWS(compact_json(claims).encode("ascii"))
    token.allowed_algs = [DEVICE_PROOF_ALGORITHM]
    try:
        token.add_signature(
            key,
            protected=compact_json(protected),
            alg=DEVICE_PROOF_ALGORITHM,
        )
        compact = token.serialize(compact=True)
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_proof_signing_failed") from exc
    if len(compact.encode("ascii")) > MAX_PROOF_BYTES:
        raise DeviceCryptoError("device_proof_too_large")
    return compact


def verify_device_proof(
    proof: str,
    *,
    method: str,
    uri: str,
    nonce: str,
    access_token: str = "",
    expected_thumbprint: str = "",
    now: int | None = None,
    window_seconds: int = DEFAULT_PROOF_WINDOW_SECONDS,
) -> VerifiedDeviceProof:
    """Verify signature, key shape, request claims, nonce, and time window."""

    compact = str(proof or "").strip()
    if not compact or len(compact.encode("utf-8")) > MAX_PROOF_BYTES:
        raise DeviceCryptoError("device_proof_invalid")
    token = jws.JWS()
    token.allowed_algs = [DEVICE_PROOF_ALGORITHM]
    try:
        token.deserialize(compact)
        header = dict(token.jose_header or {})
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_proof_invalid") from exc
    if set(header) != {"alg", "jwk", "typ"}:
        raise DeviceCryptoError("device_proof_header_invalid")
    if (
        header.get("alg") != DEVICE_PROOF_ALGORITHM
        or str(header.get("typ") or "").lower() != DEVICE_PROOF_TYPE
    ):
        raise DeviceCryptoError("device_proof_header_invalid")
    public_value = parse_public_jwk(header.get("jwk"))
    thumbprint = jwk_thumbprint(public_value)
    if expected_thumbprint and thumbprint != expected_thumbprint:
        raise DeviceCryptoError("device_proof_key_mismatch")
    try:
        token.verify(jwk.JWK(**public_value), alg=DEVICE_PROOF_ALGORITHM)
        claims = json_object(token.payload, reason="device_proof_claims_invalid")
    except DeviceCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_proof_signature_invalid") from exc

    proof_id = bounded_text(
        claims.get("jti"), reason="device_proof_jti_invalid", maximum=128
    )
    nonce_value = bounded_text(
        claims.get("nonce"), reason="device_proof_nonce_invalid", maximum=1024
    )
    expected_nonce = bounded_text(
        nonce, reason="device_proof_nonce_invalid", maximum=1024
    )
    if nonce_value != expected_nonce:
        raise DeviceCryptoError("device_proof_nonce_mismatch")
    if _request_method(claims.get("htm")) != _request_method(method):
        raise DeviceCryptoError("device_proof_method_mismatch")
    claim_uri = normalize_proof_uri(str(claims.get("htu") or ""), claim=True)
    expected_uri = normalize_proof_uri(uri)
    if claim_uri != expected_uri:
        raise DeviceCryptoError("device_proof_uri_mismatch")
    issued_at = claims.get("iat")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise DeviceCryptoError("device_proof_iat_invalid")
    moment = int(time.time() if now is None else now)
    window = max(1, min(int(window_seconds), 300))
    if issued_at < moment - window or issued_at > moment + 5:
        raise DeviceCryptoError("device_proof_iat_outside_window")
    if access_token:
        if claims.get("ath") != _access_token_hash(access_token):
            raise DeviceCryptoError("device_proof_access_token_mismatch")
    elif "ath" in claims:
        raise DeviceCryptoError("device_proof_access_token_unexpected")
    return VerifiedDeviceProof(
        thumbprint=thumbprint,
        public_jwk=public_value,
        jti=proof_id,
        issued_at=issued_at,
        method=_request_method(method),
        uri=expected_uri,
        nonce=nonce_value,
    )

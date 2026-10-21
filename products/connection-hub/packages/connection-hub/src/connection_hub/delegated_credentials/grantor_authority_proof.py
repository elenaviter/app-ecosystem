# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Admission-time proof of a Card owner's own delegable authority (W918 H1).

A project-linked agent Card is saved through the project's transaction, and
the Hub's planner then has only the owner's subject, not the roles and
permissions that decide what the owner may delegate. When the owner saves
their own Card, the Hub reads that authority from the owner's authenticated
session and signs it into a proof bound to exactly that Save.

The proof is admission-time authority: it says what the owner held when they
asked, inside one bounded transaction window. It never claims the owner still
holds it later; the Hub has no live lookup of a person's authority.

It is signed with ``connections.delegated_credentials.grantor_authority_secret``,
a secret only the Hub holds (Problem Board relays the proof but cannot make
one). Without that secret no proof is issued, and a save that would need one
is refused ``card_grantor_inventory_unavailable``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA = "connection_hub.grantor_authority_proof.v1"
MIN_SECRET_BYTES = 32
MAX_TOKEN_BYTES = 16384
PROOF_INVALID = "card_grantor_proof_invalid"
INVENTORY_UNAVAILABLE = "card_grantor_inventory_unavailable"


class GrantorProofError(ValueError):
    def __init__(self, reason: str = PROOF_INVALID) -> None:
        super().__init__(reason)
        self.reason = reason


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# The fields of an agent-Card edit a proof covers: the whole edit as forwarded, the catalog pin included.
EDIT_FIELDS = ("selection", "accepted_operations", "expected_catalog_version", "invocation_modes", "label")


def edit_of(carrier: Mapping[str, Any]) -> dict[str, Any]:
    """The edit a forwarded body or a planned update carries, in one canonical shape."""
    return {field: carrier[field] for field in EDIT_FIELDS if field in carrier}


def edit_digest(edit: Mapping[str, Any]) -> str:
    """The exact edit a proof is bound to."""
    return hashlib.sha256(_canonical(dict(edit or {}))).hexdigest()


def _key(secret: str) -> bytes:
    raw = str(secret or "").encode("utf-8")
    if len(raw) < MIN_SECRET_BYTES:
        raise GrantorProofError(INVENTORY_UNAVAILABLE)
    # A purpose-bound key, so this secret can never verify another protocol's tag.
    return hmac.new(raw, b"connection-hub/grantor-authority-proof/v1", hashlib.sha256).digest()


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
        raise GrantorProofError()
    return tuple(sorted(set(value)))


@dataclass(frozen=True)
class GrantorAuthority:
    """The verified claims of one proof."""

    grantor_subject: str
    access_id: str
    original_revision: int
    request_id: str
    edit_digest: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    issued_at: int
    expires_at: int

    def identity(self) -> dict[str, Any]:
        """The platform identity the Hub's authority inventory reads."""
        return {"user_id": self.grantor_subject, "roles": list(self.roles), "permissions": list(self.permissions)}


def issue_grantor_proof(*, secret: str, user: Mapping[str, Any], grantor_subject: str, access_id: str,
                        original_revision: int, request_id: str, edit: Mapping[str, Any],
                        now: int, ttl_seconds: int) -> str:
    """A proof for the owner's own Save. ``user`` is the Hub's authenticated session identity."""
    from connection_hub.authority_inventory import platform_identity_from_user

    identity = platform_identity_from_user(user)  # the same reading the Hub's inventory makes
    subject = identity.user_id
    if not subject or subject != grantor_subject:
        raise GrantorProofError("card_grantor_proof_not_owner")
    if type(original_revision) is not int or original_revision < 1 or type(now) is not int or now < 1:
        raise GrantorProofError()
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
        raise GrantorProofError()
    claims = {
        "schema": SCHEMA, "grantor_subject": grantor_subject, "access_id": access_id,
        "original_revision": original_revision, "request_id": request_id,
        "edit_digest": edit_digest(edit),
        "roles": sorted(set(identity.roles)),
        "permissions": sorted(set(identity.permissions)),
        "issued_at": now, "expires_at": now + ttl_seconds,
    }
    body = _b64(_canonical(claims))
    tag = _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())
    return f"{body}.{tag}"


def verify_grantor_proof(token: Any, *, secret: str, now: int, grantor_subject: str, access_id: str,
                         original_revision: int, request_id: str, edit: Mapping[str, Any]
                         ) -> GrantorAuthority:
    """The proof's claims, or ``card_grantor_proof_invalid``. Checked the same way at every phase."""
    key = _key(secret)
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_BYTES or token.count(".") != 1:
        raise GrantorProofError()
    body, tag = token.split(".")
    expected = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, tag):
        raise GrantorProofError()
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise GrantorProofError() from None
    if not isinstance(claims, dict) or claims.get("schema") != SCHEMA:
        raise GrantorProofError()
    try:
        authority = GrantorAuthority(
            grantor_subject=str(claims["grantor_subject"]), access_id=str(claims["access_id"]),
            original_revision=claims["original_revision"], request_id=str(claims["request_id"]),
            edit_digest=str(claims["edit_digest"]), roles=_strings(claims["roles"]) if claims["roles"] else (),
            permissions=_strings(claims["permissions"]) if claims["permissions"] else (),
            issued_at=claims["issued_at"], expires_at=claims["expires_at"],
        )
    except (KeyError, TypeError):
        raise GrantorProofError() from None
    if (type(authority.original_revision) is not int or type(authority.issued_at) is not int
            or type(authority.expires_at) is not int or type(now) is not int):
        raise GrantorProofError()
    if not authority.issued_at <= now < authority.expires_at:
        raise GrantorProofError()
    if (authority.grantor_subject != grantor_subject or authority.access_id != access_id
            or authority.original_revision != original_revision or authority.request_id != request_id
            or authority.edit_digest != edit_digest(edit)):
        raise GrantorProofError()
    return authority


__all__ = [
    "EDIT_FIELDS", "GrantorAuthority", "GrantorProofError", "INVENTORY_UNAVAILABLE", "PROOF_INVALID",
    "edit_digest", "edit_of", "issue_grantor_proof", "verify_grantor_proof",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: the turn token and the one-time slice seeding.

At turn start the runtime asks, with the Agent Card bearer, for a token naming
the conversation. The Hub verifies a conversation only once, when its slice is
first seeded: the KDCube registry must show it owned by the Card's grantor in
this application. The token binds (card, conversation, turn), lives about ten
minutes, is renewed only for the same turn while the registry shows that turn
alive, and never past an absolute cap. It is not a bearer: the Hub accepts it
only together with the Card bearer. A slice marked deleting gets no token and
is never reseeded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SLICE_STATE_DELETING, SliceStoreError,
)

TOKEN_SECONDS = 600
TURN_CAP_SECONDS = 7200
_PREFIX = "ts1."


class TurnRefused(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False, status: int = 403) -> None:
        super().__init__(reason)
        self.reason, self.retryable, self.status = reason, retryable, status

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.reason, "retryable": self.retryable, "status": self.status}


@dataclass(frozen=True)
class RegisteredConversation:
    owner_subject: str
    application: str
    created_at: int = 0


class ConversationRegistry(Protocol):
    """KDCube's conversation registry, as the Hub reads it."""

    async def conversation(self, *, owner_subject: str, conversation_id: str) -> RegisteredConversation | None: ...

    async def turn_alive(self, *, owner_subject: str, conversation_id: str, turn_id: str) -> bool:
        """The state row is in_progress for this turn and the conversation lock is held."""
        ...


@dataclass(frozen=True)
class TurnClaims:
    card_access_id: str
    conversation_id: str
    turn_id: str
    turn_started_at: int
    expires_at: int


class TurnTokens:
    """HMAC-sealed turn claims; the key is the Hub's own, never shared with a runtime's code."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("turn_token_key_too_short")
        self._key = key

    def _seal(self, body: bytes) -> str:
        return hmac.new(self._key, body, hashlib.sha256).hexdigest()

    def issue(self, claims: TurnClaims) -> str:
        body = json.dumps(claims.__dict__, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{_PREFIX}{base64.urlsafe_b64encode(body).decode('ascii')}.{self._seal(body)}"

    def verify(self, token: str, *, card_access_id: str, now: int) -> TurnClaims:
        try:
            encoded, seal = str(token or "")[len(_PREFIX):].rsplit(".", 1)
            body = base64.urlsafe_b64decode(encoded.encode("ascii"))
            if not str(token).startswith(_PREFIX) or not hmac.compare_digest(seal, self._seal(body)):
                raise ValueError
            claims = TurnClaims(**json.loads(body))
        except Exception:
            raise TurnRefused("turn_token_invalid") from None
        if claims.card_access_id != card_access_id:
            # Not a bearer: it names a conversation only for the Card that holds it.
            raise TurnRefused("turn_token_card_mismatch")
        if now >= claims.expires_at:
            raise TurnRefused("turn_token_expired", retryable=True, status=401)
        return claims


async def begin_turn(
    *, store: BundleStorageConversationSliceStore, registry: ConversationRegistry, tokens: TurnTokens,
    card: Any, application: str, conversation_id: str, turn_id: str, seed_selection: Mapping[str, Any],
    card_generation: int, now: int, existing_token: str = "",
) -> tuple[str, ConversationSlice]:
    """Issue or renew the turn token, seeding the slice once after the registry check."""

    if not str(turn_id or "").strip():
        raise TurnRefused("turn_id_required", status=400)
    started_at = now
    if existing_token:
        previous = tokens.verify(existing_token, card_access_id=card.access_id, now=0)
        if (previous.conversation_id, previous.turn_id) != (conversation_id, turn_id):
            raise TurnRefused("turn_token_renewal_mismatch")
        started_at = previous.turn_started_at
        if now - started_at >= TURN_CAP_SECONDS:
            raise TurnRefused("turn_token_cap_reached")
        if not await registry.turn_alive(owner_subject=card.grantor_subject, conversation_id=conversation_id,
                                         turn_id=turn_id):
            raise TurnRefused("turn_not_alive")
    # Option c (design 2026-11-24): an undecided Card transaction's marker does
    # not block the turn; the guard applies committed AND prepared meanwhile.
    current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id)
    if current is not None and current.state == SLICE_STATE_DELETING:
        raise TurnRefused("conversation_deleting", status=409)
    if current is None or current.card_generation != card_generation:
        # One registry check, when the slice is (re)seeded; never per turn.
        try:
            registered = await registry.conversation(owner_subject=card.grantor_subject,
                                                     conversation_id=conversation_id)
        except Exception:
            raise TurnRefused("conversation_registry_unavailable", retryable=True, status=503) from None
        if registered is None or registered.owner_subject != card.grantor_subject:
            raise TurnRefused("conversation_not_owned")
        if registered.application != application:
            raise TurnRefused("conversation_application_mismatch")
        seeded = ConversationSlice(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                                   conversation_id=conversation_id, card_generation=card_generation,
                                   base_card_revision=card.card_revision, selection=seed_selection)
        try:
            current = await store.write(seeded, expected_revision=current.slice_revision if current else 0)
        except SliceStoreError as exc:
            raise TurnRefused(exc.reason, retryable=True, status=409) from None
    token = tokens.issue(TurnClaims(card_access_id=card.access_id, conversation_id=conversation_id,
                                    turn_id=turn_id, turn_started_at=started_at,
                                    expires_at=min(now + TOKEN_SECONDS, started_at + TURN_CAP_SECONDS)))
    return token, current


__all__ = [
    "ConversationRegistry", "RegisteredConversation", "TOKEN_SECONDS", "TURN_CAP_SECONDS", "TurnClaims",
    "TurnRefused", "TurnTokens", "begin_turn",
]

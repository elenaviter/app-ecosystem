# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The turn token and the one-time slice seeding.

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
    BundleStorageConversationSliceStore, ConversationSlice, SliceStoreError,
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
    # The start of the conversation's latest completed deletion; 0 when never deleted.
    epoch: int = 0


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
    # The Card access id is deterministic across revoke and
    # re-create, so the token also names the Card's generation.
    card_generation: int = 0
    # "renew": ten minutes, renewed for the same live turn (a runtime that sets
    # headers per call). "per_call": up to the turn cap, for a runtime whose
    # headers are fixed for the turn; the guard checks liveness on every call.
    mode: str = "renew"


TOKEN_MODES = frozenset({"renew", "per_call"})


def card_generation_of(card: Any) -> int:
    """A Card's generation: its creation time, new only when it is revoked and created again."""
    return int(getattr(card, "created_at", 0) or 0)


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

    def verify(self, token: str, *, card_access_id: str, now: int, card_generation: int | None = None) -> TurnClaims:
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
        if card_generation is not None and claims.card_generation != card_generation:
            raise TurnRefused("turn_token_card_generation_mismatch")
        if now >= claims.expires_at:
            raise TurnRefused("turn_token_expired", retryable=True, status=401)
        return claims


TURN_TOKEN_KEY_LABEL = b"connection-hub/turn-token/v1"


def turn_tokens_from_secret(secret: str) -> "TurnTokens | None":
    """The turn-token sealer from the Hub's own grantor-authority secret.

    A purpose-bound key, as ``grantor_authority_proof._key`` derives its own, so
    neither protocol's tag can verify as the other's. None without the secret:
    the conversation path then refuses.
    """
    raw = str(secret or "").encode("utf-8")
    if len(raw) < 32:
        return None
    return TurnTokens(hmac.new(raw, TURN_TOKEN_KEY_LABEL, hashlib.sha256).digest())


async def seed_slice(store: Any, seeded: ConversationSlice, *, current: ConversationSlice | None,
                     conversation_epoch: int | None) -> ConversationSlice:
    """Write the seeded slice; when another request seeded it first, use theirs.

    Two requests of one conversation (a turn and a picker write, or two
    workers) may seed at once. The one that loses the revision race reads the
    winner instead of refusing, as long as the winner was seeded for the same
    Card generation and deletion epoch.
    """
    from connection_hub.delegated_credentials.slices.store import SliceConflict

    try:
        return await store.write(seeded, expected_revision=current.slice_revision if current else 0)
    except SliceConflict:
        winner = await store.read(grantor_subject=seeded.grantor_subject, card_access_id=seeded.card_access_id,
                                  conversation_id=seeded.conversation_id)
        if (winner is None or winner.card_generation != seeded.card_generation
                or slice_epoch_differs(winner, conversation_epoch)):
            raise
        return winner


def slice_epoch_differs(current: ConversationSlice, conversation_epoch: int | None) -> bool:
    """True when the runtime names a deletion epoch other than the one the slice was seeded under."""
    return conversation_epoch is not None and int(conversation_epoch) != int(current.conversation_epoch)


async def begin_turn(
    *, store: BundleStorageConversationSliceStore, registry: ConversationRegistry, tokens: TurnTokens,
    card: Any, application: str, conversation_id: str, turn_id: str, seed_selection: Mapping[str, Any],
    card_generation: int, now: int, existing_token: str = "", mode: str = "renew",
    conversation_epoch: int | None = None,
) -> tuple[str, ConversationSlice]:
    """Issue or renew the turn token, seeding the slice once after the registry check.

    ``conversation_epoch`` is the conversation's deletion epoch as the runtime
    sees it; a slice seeded under another epoch belongs to an earlier
    conversation with this id and is seeded again. Without it the slice is
    reused as it is.
    """

    if not str(turn_id or "").strip():
        raise TurnRefused("turn_id_required", status=400)
    if mode not in TOKEN_MODES:
        raise TurnRefused("turn_token_mode_invalid", status=400)
    started_at = now
    if existing_token:
        previous = tokens.verify(existing_token, card_access_id=card.access_id, now=0,
                                 card_generation=card_generation)
        if (previous.conversation_id, previous.turn_id) != (conversation_id, turn_id):
            raise TurnRefused("turn_token_renewal_mismatch")
        if previous.mode != "renew" or mode != "renew":
            raise TurnRefused("turn_token_not_renewable")
        started_at = previous.turn_started_at
        if now - started_at >= TURN_CAP_SECONDS:
            raise TurnRefused("turn_token_cap_reached")
        if not await registry.turn_alive(owner_subject=card.grantor_subject, conversation_id=conversation_id,
                                         turn_id=turn_id):
            raise TurnRefused("turn_not_alive")
    # An undecided Card transaction's marker does
    # not block the turn; the guard applies committed AND prepared meanwhile.
    current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id)
    if (current is None or current.card_generation != card_generation
            or slice_epoch_differs(current, conversation_epoch)):
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
                                   base_card_revision=card.card_revision, selection=seed_selection,
                                   conversation_epoch=int(getattr(registered, "epoch", 0) or 0))
        try:
            current = await seed_slice(store, seeded, current=current, conversation_epoch=conversation_epoch)
        except SliceStoreError as exc:
            raise TurnRefused(exc.reason, retryable=True, status=409) from None
    token = tokens.issue(TurnClaims(card_access_id=card.access_id, conversation_id=conversation_id,
                                    turn_id=turn_id, turn_started_at=started_at,
                                    expires_at=(started_at + TURN_CAP_SECONDS if mode == "per_call"
                                                else min(now + TOKEN_SECONDS, started_at + TURN_CAP_SECONDS)),
                                    card_generation=card_generation, mode=mode))
    return token, current


__all__ = [
    "ConversationRegistry", "RegisteredConversation", "TOKEN_SECONDS", "TURN_CAP_SECONDS", "TurnClaims",
    "TurnRefused", "TurnTokens", "begin_turn", "card_generation_of", "seed_slice", "slice_epoch_differs",
    "turn_tokens_from_secret",
]

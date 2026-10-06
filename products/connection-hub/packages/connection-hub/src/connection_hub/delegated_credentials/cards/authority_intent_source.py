"""W502 v2: the Hub's intent source and decision reader for a transaction another application initiated.

Problem Board owns the decision store of a transaction it initiates; the Hub
participant reads that transaction only through ``verify_card_authority_v2``
over a response its Card binding's authority signed for a fresh echo. Both
classes here give ``HubCardParticipant`` exactly the ports it already uses:

- ``AuthorityCardIntentSource.load(txid)``: the Hub's CardIntent, built from
  the VERIFIED candidate, with the Hub's own current Card as the original (it
  must be exactly at the candidate's base revision). It is recorded once,
  immutably, in the local intent record, so finish and recovery read the same
  intent after the authority's stage window closes.
- ``AuthorityDecisionReader.read(txid)``: a decision record carrying the
  verified persisted GlobalIntent and the recorded state; ``decision`` phase
  first, ``stage`` while it is still undecided.

The transport (``fetch``) and the authority's secret, service id and audience
are bound by the composition root from the Card's trusted binding, never by a
caller. Nothing here is module state.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused, GlobalIntent

from .card_participant import PARTICIPANT, CardIntent, LocalCardIntentSource
from .model import CardAuthority
from .transaction_authority_v2 import TransactionAuthorityRefused, VerifiedCardAuthority, verify_card_authority_v2

# fetch(transaction_id, phase, request_echo) -> the authority's response
# mapping; a remote refusal raises TransactionAuthorityRefused(reason).
AuthorityFetch = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class CardAuthorityBinding:
    """The trusted authority of one Card binding, resolved by the composition root."""

    secret: str | bytes = field(repr=False)
    service_id: str
    audience: str


@dataclass(frozen=True)
class AuthorityDecisionRecord:
    """The ``DecisionRecord`` view the Hub participant reads (no prepared/finished maps)."""

    intent: GlobalIntent
    state: str
    decided_at: int | None = None
    prepared: Mapping[str, Any] = field(default_factory=dict)
    finished: Mapping[str, Any] = field(default_factory=dict)

    @property
    def transaction_id(self) -> str:
        return self.intent.transaction_id

    @property
    def terminal(self) -> bool:
        return self.state in ("committed", "aborted")


class _AuthorityReads:
    def __init__(self, *, fetch: AuthorityFetch, authority: CardAuthorityBinding,
                 clock: Callable[[], float] = time.time, echo: Callable[[], str] = lambda: secrets.token_hex(16)) -> None:
        self._fetch = fetch
        self._authority = authority
        self._clock = clock
        self._echo = echo

    async def verified(self, transaction_id: str, phase: str) -> VerifiedCardAuthority:
        request_echo = self._echo()  # fresh per request: an old response can never verify
        try:
            response = await self._fetch(transaction_id, phase, request_echo)
        except TransactionAuthorityRefused:
            raise
        except Exception:  # noqa: BLE001 - transport failure, by name only
            raise TransactionAuthorityRefused("authority_unavailable") from None
        return verify_card_authority_v2(
            response, secret=self._authority.secret, service_id=self._authority.service_id,
            audience=self._authority.audience, participant=PARTICIPANT, transaction_id=transaction_id,
            phase=phase, request_echo=request_echo, now=int(self._clock()))


class AuthorityDecisionReader(_AuthorityReads):
    async def read(self, transaction_id: str) -> AuthorityDecisionRecord | None:
        try:
            verified = await self.verified(transaction_id, "decision")
        except TransactionAuthorityRefused as exc:
            if exc.reason == "authority_transaction_unknown":
                return None
            if exc.reason != "authority_decision_pending":
                raise DecisionRefused(exc.reason) from None
            try:
                verified = await self.verified(transaction_id, "stage")
            except TransactionAuthorityRefused as stage_exc:
                raise DecisionRefused(stage_exc.reason) from None
            return AuthorityDecisionRecord(intent=verified.intent, state="preparing")
        return AuthorityDecisionRecord(intent=verified.intent, state=verified.decision,
                                       decided_at=verified.decided_at)


class AuthorityCardIntentSource(_AuthorityReads):
    def __init__(self, *, store: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._local = LocalCardIntentSource(store)

    async def load(self, transaction_id: str) -> CardIntent:
        try:
            return await self._local.load(transaction_id)  # already verified and recorded once
        except DecisionRefused as exc:
            if str(exc) != "card_intent_unknown":
                raise
        try:
            verified = await self.verified(transaction_id, "stage")
        except TransactionAuthorityRefused as exc:
            # A closed stage window with no local record: nothing was staged here.
            raise DecisionRefused("card_intent_unknown" if exc.reason == "authority_late_stage" else exc.reason) from None
        projection, value = verified.projection, verified.candidate
        subject_hash = projection["target_scope"]
        if type(subject_hash) is not str or not _HEX64.fullmatch(subject_hash):
            # The Hub's storage scope, sha256(grantor_subject) hex (EMain C1); never a raw subject.
            raise DecisionRefused("card_intent_not_bound")
        current = await self._store.read_current_authority(subject_hash=subject_hash, access_id=value["access_id"])
        if current is None or current[1].card_revision != value["original_revision"]:
            raise DecisionRefused("card_intent_base_moved")
        try:
            candidate = CardAuthority.from_mapping(value["candidate"])
        except (KeyError, TypeError, ValueError):
            raise DecisionRefused("card_intent_invalid") from None
        intent = CardIntent(transaction_id=transaction_id, intent_digest=verified.intent.digest,
                            subject_hash=subject_hash, original=current[1], candidate=candidate,
                            effects=tuple(dict(effect) for effect in value["effects"]),
                            action=projection["action"], actor_subject=projection["actor_subject"],
                            actor_kind=projection["actor_kind"])
        await self._local.record(intent)
        return intent


__all__ = ["AuthorityCardIntentSource", "AuthorityDecisionReader", "AuthorityDecisionRecord", "AuthorityFetch",
           "CardAuthorityBinding"]

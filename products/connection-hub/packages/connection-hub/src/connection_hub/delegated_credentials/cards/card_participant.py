"""W578: the Hub's Card participant in the ONE generalized commit protocol (W581 kernel).

The generic ``Coordinator`` runs prepare, one durable decision and finish for
one participant or several; this module is the Hub's ``Participant`` for it.
It never takes a candidate from the caller of ``prepare``: it resolves the
transaction's exact intent BY TRANSACTION ID from a trusted intent source,
stages it through the existing W578 participant (fence, serving marker,
prepared receipt with its effects), and on ``finish`` materializes exactly
the decision the coordinator's store recorded (applying effects only for
COMMITTED). One-Card and multi-Card edits use this same code.

Intent sources:
- ``LocalCardIntentSource``: an edit the Hub itself initiates writes its
  immutable intent record (candidate, effects, actor) before the coordinator
  prepares; the participant reads only that record.
- A verified v2 pull from the binding's authority (H-T) is the second
  implementation of the same port, for edits another application initiates.
"""

from __future__ import annotations

import re

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused, GlobalIntent, Receipt
from service_foundation.coordination.durable_wire import (
    WireRefused, canonical_json_bytes, participant_projection, projection_digest, sha256_hex,
)

from ..durable_io import read_json_or_none, write_json_atomic
from .model import CardAuthority
from .transaction_store import CardTransactionRefused, list_in_doubt, state as read_state

PARTICIPANT = "connection-hub.card"
INTENT_RECORD_SCHEMA = "connection-hub.card-intent.v1"
GROUP_INTENT_RECORD_SCHEMA = "connection-hub.card-group-intent.v1"


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(receipt))).hexdigest()


def dependency_revisions(reads: Sequence[Mapping[str, Any]] = (), *, catalog_version_digest: str = "",
                         ) -> dict[str, int]:
    """The projection's ``dependency_revisions`` for read reservations (W502; kernel: values int >= 1).

    ``card:<subject_hash>:<access_id>`` -> the unchanged Card's exact revision;
    ``card-absent:<subject_hash>:<access_id>`` -> 1, meaning that Card must not exist;
    ``catalog-active:<sha256(version)>`` -> 1, meaning the active catalog is exactly
    that version until the transaction is decided (the initiator's check evaluated it).
    """
    result: dict[str, int] = {}
    if catalog_version_digest:
        result[f"catalog-active:{catalog_version_digest}"] = 1
    for read in reads:
        if read["revision"] == 0:
            result[f"card-absent:{read['subject_hash']}:{read['access_id']}"] = 1
        else:
            result[f"card:{read['subject_hash']}:{read['access_id']}"] = int(read["revision"])
    return result


def reads_from_dependencies(dependencies: Any) -> list[dict[str, Any]]:
    """The read reservations a projection names, or a named refusal for any other key.

    A reservation is exclusive: while one transaction is prepared on a
    dependency Card, another that reads or writes it is refused
    (card_dependency_reserved). Within one Problem Board project the
    board's own fence already serializes; this is only seen across projects
    that share a Control (EMain #603).
    """
    if not isinstance(dependencies, Mapping):
        raise DecisionRefused("card_dependency_invalid")
    reads = []
    for key, value in dependencies.items():
        if type(key) is str and key.startswith("catalog-active:"):
            continue  # catalog_reservation_from_dependencies validates it
        parts = key.split(":", 2) if type(key) is str else []
        if len(parts) != 3 or not parts[1] or not parts[2] or type(value) is not int:
            raise DecisionRefused("card_dependency_invalid")
        if parts[0] == "card" and value >= 1:
            reads.append({"subject_hash": parts[1], "access_id": parts[2], "revision": value})
        elif parts[0] == "card-absent" and value == 1:
            reads.append({"subject_hash": parts[1], "access_id": parts[2], "revision": 0})
        else:
            raise DecisionRefused("card_dependency_invalid")
    return sorted(reads, key=lambda read: (read["subject_hash"], read["access_id"]))


def catalog_reservation_from_dependencies(dependencies: Any) -> str:
    """The active catalog version digest a projection reserves ("" when none), or a named refusal."""
    if not isinstance(dependencies, Mapping):
        raise DecisionRefused("card_dependency_invalid")
    found = [(key, value) for key, value in dependencies.items()
             if type(key) is str and key.startswith("catalog-active:")]
    if not found:
        return ""
    key, value = found[0]
    digest = key[len("catalog-active:"):]
    if len(found) != 1 or type(value) is not int or value != 1 or not _HEX64.fullmatch(digest):
        raise DecisionRefused("card_dependency_invalid")
    return digest


def hub_participant_input(*, original: CardAuthority, candidate: CardAuthority, subject_hash: str,
                          action: str, actor_subject: str, actor_kind: str,
                          effects: Sequence[Mapping[str, Any]] = (),
                          reads: Sequence[Mapping[str, Any]] = (),
                          catalog_version_digest: str = "") -> dict[str, Any]:
    """The Hub's v2 ``participant_inputs[PARTICIPANT]`` for one Card change (W581 v2 kernel).

    This is the ONLY shape the Hub stages (EMain C1, 2026-10-06): an initiator
    such as Problem Board writes exactly these values; the shared vectors in
    ``tests/fixtures/w502_hub_participant_vectors.json`` pin them for both sides.

    - ``binding_kind`` is ``connection-hub.card``; ``binding_ref`` the access id;
      ``target_scope`` the Card's storage scope, sha256(grantor_subject) hex.
    - ``target_incarnation`` is max(1, base Card revision). A Card is never
      recreated under the same access id (ids are minted, revisions only grow,
      a revoked Card keeps its id), so the revision is the incarnation
      (EMain N2); it adds no identity beyond ``before_revision``.
    - ``dependency_revisions`` is ``{}``; ``provisioning`` is ``{}``.
    - ``actor_kind`` is ``caller`` (an authenticated actor other than the
      grantor, such as a project admin) or ``grantor``; the initiator's own
      projection keeps its own vocabulary (human/agent), never normalized here.
    - ``candidate_digest`` binds the base revision, the complete candidate and
      every effect (Ops B1), so the one global digest decides on exactly what
      the Hub applies. The actor is the authenticated caller's, never a payload's.
    """
    return {
        "participant": PARTICIPANT, "binding_kind": "connection-hub.card", "binding_ref": original.access_id,
        "target_scope": subject_hash, "target_incarnation": max(1, original.card_revision), "action": action,
        "before_revision": original.card_revision, "candidate_revision": original.card_revision + 1,
        "candidate_digest": card_intent_payload_digest(original=original, candidate=candidate, effects=effects),
        "dependency_revisions": dependency_revisions(reads, catalog_version_digest=catalog_version_digest),
        "actor_subject": actor_subject, "actor_kind": actor_kind,
        "provisioning": {},
    }


def hub_projection(intent: GlobalIntent) -> dict[str, Any]:
    """The Hub's exact projection of a global intent, or a named refusal."""
    try:
        return participant_projection(intent, PARTICIPANT)
    except WireRefused as exc:
        raise DecisionRefused(str(exc)) from exc


def card_intent_payload_digest(*, original: CardAuthority, candidate: CardAuthority,
                               effects: Sequence[Mapping[str, Any]] = ()) -> str:
    """The Hub projection's ``candidate_digest`` for this Card change (Ops B1; W502 v2).

    sha256 of the kernel's canonical bytes of {access_id, original_revision,
    candidate, effects}, effect order kept: the value Apps freezes as
    ``participant_candidates["connection-hub.card"]`` and hashes the same way
    (CodeApp 16:58). It binds the base revision, the complete candidate and
    every effect, so the one global decision covers exactly what the Hub applies.
    """
    return candidate_value_digest(candidate_value(original=original, candidate=candidate, effects=effects))


def candidate_value(*, original: CardAuthority, candidate: CardAuthority,
                    effects: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {"access_id": original.access_id, "original_revision": original.card_revision,
            "candidate": candidate.to_dict(), "effects": [dict(effect) for effect in effects]}


def candidate_value_digest(value: Mapping[str, Any]) -> str:
    try:
        return sha256_hex(canonical_json_bytes(dict(value)))
    except WireRefused as exc:
        raise DecisionRefused(str(exc)) from exc


@dataclass(frozen=True)
class CardIntent:
    """What one transaction may do to one Card; immutable once recorded."""

    transaction_id: str
    intent_digest: str
    subject_hash: str
    original: CardAuthority
    candidate: CardAuthority
    effects: tuple[Mapping[str, Any], ...] = ()
    # The authenticated action and actor this change was staged under (CodeApp
    # 17:25): kept with the intent and compared EXACTLY with the projection.
    action: str = ""
    actor_subject: str = ""
    actor_kind: str = ""
    # W502 read reservations: unchanged dependency Cards held through finish.
    reads: tuple[Mapping[str, Any], ...] = ()
    # W502 inbound participant: the configured authority that staged this
    # intent ("" = the Hub's own decision store) and the verified scope value
    # it was staged under. Readers route the decision by the authority.
    authority: str = ""
    scope: str = ""
    # W502: the active catalog version digest this transaction reserves ("" when none).
    catalog: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"schema": INTENT_RECORD_SCHEMA, "transaction_id": self.transaction_id,
                "intent_digest": self.intent_digest, "subject_hash": self.subject_hash,
                "original": self.original.to_dict(), "candidate": self.candidate.to_dict(),
                "effects": [dict(effect) for effect in self.effects], "action": self.action,
                "actor_subject": self.actor_subject, "actor_kind": self.actor_kind,
                "reads": [dict(read) for read in self.reads], "authority": self.authority, "scope": self.scope,
                "catalog": self.catalog}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CardIntent":
        if not isinstance(raw, Mapping) or raw.get("schema") != INTENT_RECORD_SCHEMA:
            raise DecisionRefused("card_intent_invalid")
        try:
            return cls(transaction_id=raw["transaction_id"], intent_digest=raw["intent_digest"],
                       subject_hash=raw["subject_hash"], original=CardAuthority.from_mapping(raw["original"]),
                       candidate=CardAuthority.from_mapping(raw["candidate"]),
                       effects=tuple(dict(effect) for effect in raw.get("effects") or ()),
                       action=str(raw.get("action") or ""), actor_subject=str(raw.get("actor_subject") or ""),
                       actor_kind=str(raw.get("actor_kind") or ""),
                       reads=tuple(dict(read) for read in raw.get("reads") or ()),
                       authority=str(raw.get("authority") or ""), scope=str(raw.get("scope") or ""),
                       catalog=str(raw.get("catalog") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionRefused("card_intent_invalid") from exc


@dataclass(frozen=True)
class CardGroupMemberIntent:
    """One member of a card group: its storage scope, its original (None when newly minted), its candidate."""

    subject_hash: str
    original: CardAuthority | None
    candidate: CardAuthority
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {"subject_hash": self.subject_hash, "action": self.action, "candidate": self.candidate.to_dict(),
                "original": self.original.to_dict() if self.original is not None else None}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CardGroupMemberIntent":
        if not isinstance(raw, Mapping) or set(raw) != {"subject_hash", "action", "candidate", "original"}:
            raise DecisionRefused("card_intent_invalid")
        return cls(subject_hash=str(raw["subject_hash"]), action=str(raw["action"]),
                   candidate=CardAuthority.from_mapping(raw["candidate"]),
                   original=CardAuthority.from_mapping(raw["original"]) if raw["original"] is not None else None)


@dataclass(frozen=True)
class CardGroupIntent:
    """W578: what one transaction may do to several Cards; immutable once recorded.

    The members are in the group's canonical (subject_hash, access_id) order;
    the group's reads, catalog and effects are staged on its lead member.
    """

    transaction_id: str
    intent_digest: str
    members: tuple[CardGroupMemberIntent, ...]
    effects: tuple[Mapping[str, Any], ...] = ()
    actor_subject: str = ""
    actor_kind: str = ""
    reads: tuple[Mapping[str, Any], ...] = ()
    authority: str = ""
    scope: str = ""
    catalog: str = ""

    def candidate_value(self) -> dict[str, Any]:
        from .card_group import group_candidate_value, group_member
        return group_candidate_value([group_member(original=member.original, candidate=member.candidate,
                                                   action=member.action) for member in self.members],
                                     self.effects)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": GROUP_INTENT_RECORD_SCHEMA, "transaction_id": self.transaction_id,
                "intent_digest": self.intent_digest, "members": [member.to_dict() for member in self.members],
                "effects": [dict(effect) for effect in self.effects], "actor_subject": self.actor_subject,
                "actor_kind": self.actor_kind, "reads": [dict(read) for read in self.reads],
                "authority": self.authority, "scope": self.scope, "catalog": self.catalog}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CardGroupIntent":
        if not isinstance(raw, Mapping) or raw.get("schema") != GROUP_INTENT_RECORD_SCHEMA:
            raise DecisionRefused("card_intent_invalid")
        try:
            return cls(transaction_id=raw["transaction_id"], intent_digest=raw["intent_digest"],
                       members=tuple(CardGroupMemberIntent.from_mapping(member) for member in raw["members"]),
                       effects=tuple(dict(effect) for effect in raw.get("effects") or ()),
                       actor_subject=str(raw.get("actor_subject") or ""),
                       actor_kind=str(raw.get("actor_kind") or ""),
                       reads=tuple(dict(read) for read in raw.get("reads") or ()),
                       authority=str(raw.get("authority") or ""), scope=str(raw.get("scope") or ""),
                       catalog=str(raw.get("catalog") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionRefused("card_intent_invalid") from exc


def intent_from_mapping(raw: Any) -> "CardIntent | CardGroupIntent":
    """A recorded intent of either shape, by its schema."""
    if isinstance(raw, Mapping) and raw.get("schema") == GROUP_INTENT_RECORD_SCHEMA:
        return CardGroupIntent.from_mapping(raw)
    return CardIntent.from_mapping(raw)


class CardIntentSource(Protocol):
    async def load(self, transaction_id: str) -> CardIntent: ...


class LocalCardIntentSource:
    """The durable intent of an edit the Hub itself initiates; written once, before prepare."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _path(self, transaction_id: str):
        from .transaction_store import _checked_id
        return self._store.root / "card-transactions" / "intents" / f"{_checked_id(transaction_id)}.json"

    async def record(self, intent: "CardIntent | CardGroupIntent") -> None:
        path = self._path(intent.transaction_id)
        existing = await read_json_or_none(path)
        if existing is not None:
            if existing != intent.to_dict():
                raise DecisionRefused("card_intent_conflict")  # immutable: an exact replay only
            return
        await write_json_atomic(path, intent.to_dict())

    async def load(self, transaction_id: str) -> "CardIntent | CardGroupIntent":
        raw = await read_json_or_none(self._path(transaction_id))
        if raw is None:
            raise DecisionRefused("card_intent_unknown")
        intent = intent_from_mapping(raw)
        if intent.transaction_id != transaction_id:
            raise DecisionRefused("card_intent_invalid")
        return intent


class DecisionStorePort:
    """The Hub participant's TransactionDecisionPort over the coordinator's own store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def decision(self, receipt: Mapping[str, Any]) -> str:
        record = await self._store.read(receipt["transaction_id"])
        if record is None or record.intent.digest != receipt["intent_digest"]:
            return "undecided"
        return record.state if record.terminal else "undecided"


class HubCardParticipant:
    """The Hub's ``Participant``; bound by the composition root, never chosen by a caller."""

    name = PARTICIPANT

    def __init__(self, *, service: Any, store: Any, intents: CardIntentSource, decisions: Any,
                 now: Any = None) -> None:
        self._service = service
        self._store = store
        self._intents = intents
        self._decisions = decisions
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def _receipt(self, local: Mapping[str, Any]) -> Receipt:
        """The v2 receipt: the coordinator checks epoch, global, projection and candidate digests."""
        record = await self._decisions.read(local["transaction_id"])
        if record is None or record.intent.digest != local["intent_digest"]:
            raise DecisionRefused("card_intent_not_bound")
        projection = hub_projection(record.intent)
        return Receipt(local["transaction_id"], record.intent.epoch, record.intent.digest, PARTICIPANT,
                       projection_digest(record.intent, PARTICIPANT), projection["candidate_digest"],
                       receipt_digest(local))

    async def _bound_intent(self, transaction_id: str) -> "CardIntent | CardGroupIntent":
        """The Hub intent, refused unless it is exactly what the coordinator's Intent names."""
        intent = await self._intents.load(transaction_id)
        record = await self._decisions.read(transaction_id)
        if record is None:
            raise DecisionRefused("transaction_unknown")
        if record.intent.digest != intent.intent_digest or PARTICIPANT not in record.intent.participants:
            raise DecisionRefused("card_intent_not_bound")
        projection = hub_projection(record.intent)
        if isinstance(intent, CardGroupIntent):
            return self._bound_group(intent, projection)
        expected = card_intent_payload_digest(original=intent.original, candidate=intent.candidate,
                                              effects=intent.effects)
        # The global intent names exactly this Card change. Every projection
        # field the Hub acts on is compared, not only the candidate digest
        # (Ops, 16:55): a matching digest with another revision, target or
        # incarnation must not stage.
        if (projection["candidate_digest"] != expected
                or projection["participant"] != PARTICIPANT
                or projection["binding_kind"] != "connection-hub.card"
                or projection["binding_ref"] != intent.original.access_id
                or projection["target_scope"] != intent.subject_hash
                or projection["before_revision"] != intent.original.card_revision
                or projection["candidate_revision"] != intent.original.card_revision + 1
                or projection["target_incarnation"] != max(1, intent.original.card_revision)
                or reads_from_dependencies(projection["dependency_revisions"]) != sorted(
                    (dict(read) for read in intent.reads), key=lambda read: (read["subject_hash"], read["access_id"]))
                or catalog_reservation_from_dependencies(projection["dependency_revisions"]) != intent.catalog
                or not intent.action or projection["action"] != intent.action
                or not intent.actor_subject or projection["actor_subject"] != intent.actor_subject
                or projection["actor_kind"] not in ("caller", "grantor")
                or projection["actor_kind"] != intent.actor_kind):
            raise DecisionRefused("card_intent_not_bound")
        return intent

    @staticmethod
    def _bound_group(intent: "CardGroupIntent", projection: Mapping[str, Any]) -> "CardGroupIntent":
        """W578: every aggregate field and member the group intent stages, compared with the projection."""
        from .card_group import verify_group_projection
        if projection.get("binding_kind") != "connection-hub.card-group":
            raise DecisionRefused("card_intent_not_bound")
        verify_group_projection(projection, intent.candidate_value())
        if (reads_from_dependencies(projection["dependency_revisions"]) != sorted(
                (dict(read) for read in intent.reads), key=lambda read: (read["subject_hash"], read["access_id"]))
                or catalog_reservation_from_dependencies(projection["dependency_revisions"]) != intent.catalog
                or not intent.actor_subject or projection["actor_subject"] != intent.actor_subject
                or projection["actor_kind"] != intent.actor_kind):
            raise DecisionRefused("card_intent_not_bound")
        return intent

    async def prepare(self, transaction_id: str) -> Receipt:
        intent = await self._bound_intent(transaction_id)
        if isinstance(intent, CardGroupIntent):
            try:
                prepared = await self._service.stage_group_transaction(
                    transaction_id=transaction_id, intent_digest=intent.intent_digest, participant=PARTICIPANT,
                    members=[(member.subject_hash, member.original, member.candidate, member.action)
                             for member in intent.members],
                    now=self._now(), effects=intent.effects, reads=intent.reads, catalog=intent.catalog)
            except CardTransactionRefused as exc:
                raise DecisionRefused(str(exc)) from exc
            return await self._receipt(prepared)
        try:
            prepared = await self._service.stage_transaction(
                transaction_id=transaction_id, intent_digest=intent.intent_digest, participant=PARTICIPANT,
                subject_hash=intent.subject_hash, original=intent.original, candidate=intent.candidate,
                now=self._now(), effects=intent.effects, reads=intent.reads, catalog=intent.catalog)
        except CardTransactionRefused as exc:
            raise DecisionRefused(str(exc)) from exc
        return await self._receipt(prepared)

    async def finish(self, transaction_id: str, decision: str) -> Receipt:
        try:
            intent = await self._intents.load(transaction_id)
        except DecisionRefused as exc:
            if str(exc) != "card_intent_unknown" or decision != "aborted":
                raise
            # Ops R1: the initiator crashed between the coordinator's begin and
            # recording this intent. Without an intent nothing was ever staged
            # here (prepare needs it), so the ABORT is a tombstone by id; a late
            # stage is refused anyway by the recorded decision.
            from .transaction_store import abort_unstaged
            record = await self._decisions.read(transaction_id)
            if record is None or record.state != "aborted":
                raise
            tombstone = await abort_unstaged(self._store, transaction_id, intent_digest=record.intent.digest)
            return await self._tombstone_receipt(record, tombstone)
        if isinstance(intent, CardGroupIntent):
            return await self._finish_group(intent, transaction_id, decision)
        if decision == "aborted" and await read_state(self._store, transaction_id=transaction_id) is None:
            # Never durably prepared here (a lost prepare reply, or a stage
            # that crashed first): an idempotent abort tombstone (W581 F1),
            # written under the Card's own section so it cannot race a stage.
            tombstone = await self._service.abort_unstaged_transaction(
                transaction_id=transaction_id, subject_hash=intent.subject_hash,
                access_id=intent.original.access_id, intent_digest=intent.intent_digest)
            if tombstone.get("state") != "aborted":  # the stage won the section: finish its receipt
                return await self.finish(transaction_id, decision)
            record = await self._decisions.read(transaction_id)
            if record is None or record.intent.digest != intent.intent_digest:
                raise DecisionRefused("card_intent_not_bound")
            return await self._tombstone_receipt(record, tombstone)
        try:
            decided = await self._service.decide_transaction(
                transaction_id=transaction_id, intent_digest=intent.intent_digest, decision=decision,
                subject_hash=intent.subject_hash, access_id=intent.original.access_id)
        except CardTransactionRefused as exc:
            raise DecisionRefused(str(exc)) from exc
        return await self._receipt(decided)

    async def _finish_group(self, intent: "CardGroupIntent", transaction_id: str, decision: str) -> Receipt:
        """W578: an unstaged group aborts by tombstone under its lead's section; otherwise every member."""
        if decision == "aborted" and await read_state(self._store, transaction_id=transaction_id) is None:
            lead = intent.members[0]
            tombstone = await self._service.abort_unstaged_transaction(
                transaction_id=transaction_id, subject_hash=lead.subject_hash,
                access_id=lead.candidate.access_id, intent_digest=intent.intent_digest)
            if tombstone.get("state") != "aborted" or tombstone.get("schema"):  # a stage won the section
                return await self._finish_group(intent, transaction_id, decision)
            record = await self._decisions.read(transaction_id)
            if record is None or record.intent.digest != intent.intent_digest:
                raise DecisionRefused("card_intent_not_bound")
            return await self._tombstone_receipt(record, tombstone)
        try:
            decided = await self._service.decide_group_transaction(
                transaction_id=transaction_id, intent_digest=intent.intent_digest, decision=decision)
        except CardTransactionRefused as exc:
            raise DecisionRefused(str(exc)) from exc
        return await self._receipt(decided)

    @staticmethod
    async def _tombstone_receipt(record: Any, tombstone: Mapping[str, Any]) -> Receipt:
        projection = hub_projection(record.intent)
        return Receipt(record.transaction_id, record.intent.epoch, record.intent.digest, PARTICIPANT,
                       projection_digest(record.intent, PARTICIPANT), projection["candidate_digest"],
                       receipt_digest(tombstone))

    async def read_pending(self, transaction_id: str) -> Receipt | None:
        receipt = await read_state(self._store, transaction_id=transaction_id)
        return await self._receipt(receipt) if _prepared_as_a_whole(receipt) else None

    async def list_prepared(self, *, limit: int) -> Sequence[Receipt]:
        listed = await list_in_doubt(self._store)
        if len(listed) > limit:
            raise DecisionRefused("recovery_unbounded")
        result = []
        for entry in listed:
            receipt = await read_state(self._store, transaction_id=entry["transaction_id"])
            if _prepared_as_a_whole(receipt):
                result.append(await self._receipt(receipt))
        return result


def _prepared_as_a_whole(receipt: Mapping[str, Any] | None) -> bool:
    """A prepare acknowledgement: a prepared Card, or a group only once EVERY member is staged."""
    if receipt is None or receipt["state"] != "prepared":
        return False
    return bool(receipt["staged"]) if "staged" in receipt else True


class HubLocalReceiptVerifier:
    """The ``ReceiptVerifier`` for a transaction only the Hub takes part in.

    The receipt's digest must equal the Hub's own durable receipt for that
    transaction; the coordinator has already checked epoch, global, projection
    and candidate digests against the stored intent.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def _check(self, receipt: Receipt, states: tuple[str, ...]) -> None:
        if receipt.participant != PARTICIPANT:
            raise DecisionRefused("receipt_participant_unknown")
        local = await read_state(self._store, transaction_id=receipt.transaction_id)
        if local is None:
            from .transaction_store import read_receipt
            local = await read_receipt(self._store, receipt.transaction_id)
        if local is None and "aborted" in states:
            # The Hub's own abort tombstone (R1: no intent recorded; F1: never
            # staged) is its durable receipt for an ABORT finish (EMain 18:35).
            from .transaction_store import tombstone_path
            tombstone = await read_json_or_none(tombstone_path(self._store, receipt.transaction_id))
            if isinstance(tombstone, Mapping) and tombstone.get("transaction_id") == receipt.transaction_id:
                local = tombstone
        if local is None or local.get("state") not in states or receipt_digest(local) != receipt.receipt_digest:
            raise DecisionRefused("receipt_unauthenticated")

    async def prepared(self, record: Any, receipt: Receipt) -> None:
        await self._check(receipt, ("prepared",))

    async def finished(self, record: Any, receipt: Receipt) -> None:
        await self._check(receipt, (record.state,))


__all__ = ["CardGroupIntent", "CardGroupMemberIntent", "CardIntent", "CardIntentSource",
           "GROUP_INTENT_RECORD_SCHEMA", "intent_from_mapping", "DecisionStorePort", "HubCardParticipant", "HubLocalReceiptVerifier",
           "LocalCardIntentSource", "PARTICIPANT", "candidate_value", "candidate_value_digest", "dependency_revisions",
           "reads_from_dependencies", "catalog_reservation_from_dependencies",
           "card_intent_payload_digest", "hub_participant_input",
           "hub_projection", "receipt_digest"]

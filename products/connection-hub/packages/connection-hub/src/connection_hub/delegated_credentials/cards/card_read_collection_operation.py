"""W502 lane D: ``card_read_collection_register`` seals a project step's Card reads as a Hub collection.

The initiator (Problem Board) names the scope and the persons from its own fenced
membership, exactly as for ``card_census_read``: completeness of the person list
stays the initiator's. The Hub derives each person's descriptors from its OWN
Card store through the census identity path (``CardCensusReadOperation._person``),
so the descriptors equal the initiator's ``read_reservations()`` for the same
census:

- the person's My Card and project Control, present at their revision or an
  authoritative absence (revision 0);
- every Card of a ``complete`` upstream chain, present at its revision;
- distinct across persons, sorted by (subject_hash, access_id).

Any Card under another decision (``in_transaction``), an unavailable chain or an
invalid chain refuses the WHOLE registration by name: a collection is never
sealed partially. The catalog dependency is the ACTIVE catalog's version digest.

The collection id is ``sha256({service_id, scope, request_id})[:32]``: a retry of
the same request reseals idempotently, and different reads under the same id
refuse ``card_read_collection_conflict``. The signed answer carries only the
bounded reference (``connection-hub.card-read-collection-ref.v1``), never the reads.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex
from service_foundation.coordination.participant_answer import AnswerContract
from service_foundation.coordination.participant_answer import request_digest as shared_request_digest
from service_foundation.coordination.participant_answer import sign_participant_answer

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from ..catalog.reservations import catalog_version_digest
from .card_read_collection import MAX_COLLECTION_READS, seal_collection
from .card_read_set import READ_COLLECTION_REF_SCHEMA, validate_read_collection_ref
from .census_read import MAX_PERSONS, CardCensusReadOperation, _Refused, _unsigned_refusal
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller
from .store import CardStorageError

OPERATION = "card_read_collection_register"
REQUEST_SCHEMA = "card-read-collection-register.v1"
ANSWER_SCHEMA = "card-read-collection-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "request_echo", "scope", "persons", "actor_subject", "request_id", "deadline")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_BOUNDED = 256


def collection_request_digest(request: Mapping[str, Any]) -> str:
    """The shared helper's digest under the closed collection contract."""
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS},
                                 contract=AnswerContract.COLLECTION)


def collection_id_for(*, service_id: str, scope: str, request_id: str) -> str:
    """Stable per (caller, scope, request): a retry reseals the same collection."""
    return sha256_hex(canonical_json_bytes({"service_id": service_id, "scope": scope,
                                            "request_id": request_id}))[:32]


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable() and value == value.strip()


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof, persons = data["service_proof"], data["persons"]
    return (isinstance(proof, Mapping) and set(proof) == PROOF_FIELDS
            and all(type(proof[name]) is str for name in PROOF_FIELDS)
            and data["schema"] == REQUEST_SCHEMA and type(data["request_echo"]) is str
            and bool(_ECHO.fullmatch(data["request_echo"])) and _bounded(data["scope"])
            and _bounded(data["actor_subject"]) and _bounded(data["request_id"])
            and type(data["deadline"]) is int and data["deadline"] > 0
            and type(persons) is list and 0 < len(persons) <= MAX_PERSONS
            and all(_bounded(person) for person in persons)
            and persons == sorted(set(persons)))  # sorted and unique: one canonical request


class CardReadCollectionOperation(CardCensusReadOperation):
    """``answer(data)`` for ``card_read_collection_register``; bound by the composition root.

    Admitted exactly like ``card_census_read`` (caller descriptor, ``census_scope_prefix``,
    single-use nonce, request frozen before any await), with its own operation, schemas
    and answer contract.
    """

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], card_store: Any, catalog_store: Any,
                 nonces: Any, clock, nonce_prefix: str = "connection-hub:card-read-collection:nonce:") -> None:
        super().__init__(callers=callers, card_store=card_store, catalog_store=catalog_store, nonces=nonces,
                         clock=clock, nonce_prefix=nonce_prefix)

    async def answer(self, data: Any) -> dict[str, Any]:
        try:  # frozen before any await
            data = json.loads(json.dumps(dict(data), allow_nan=False)) if isinstance(data, Mapping) else None
        except (TypeError, ValueError):
            data = None
        if not _valid_request(data):
            return _unsigned_refusal("card_read_collection_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = collection_request_digest(data)
        proof = ServiceProof(**{name: raw_proof[name] for name in PROOF_FIELDS})
        verdict = verify_admission_request(
            secret=caller.request_secret, proof=proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
            request=AdmissionRequest(resource=caller.hub_resource, operation=OPERATION,
                                     invocation_id=data["request_echo"], request_digest=digest,
                                     approval_context={"protocol": REQUEST_SCHEMA}),
            max_clock_skew_seconds=MAX_SKEW_SECONDS, now=int(self._clock()))
        if not verdict.allowed:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            fresh = await self._nonces.set(f"{self._nonce_prefix}{caller.service_id}:{proof.nonce}", "1",
                                           ex=2 * MAX_SKEW_SECONDS, nx=True)
        except Exception:  # noqa: BLE001
            return _unsigned_refusal("card_participant_unavailable", 503)
        if not fresh:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            prefix = caller.census_scope_prefix
            if not prefix or not data["scope"].startswith(prefix):
                raise _Refused("card_census_scope_forbidden", 403)
            if data["deadline"] <= int(self._clock()):
                raise _Refused("card_read_collection_deadline_passed", 409)
            reads = await self._reads(data["scope"], data["persons"])
            catalog = await self._catalog_digest()
            collection_id = collection_id_for(service_id=caller.service_id, scope=data["scope"],
                                              request_id=data["request_id"])
            try:
                header = await seal_collection(self._cards, collection_id=collection_id, scope=data["scope"],
                                               actor_subject=data["actor_subject"], request_id=data["request_id"],
                                               deadline=data["deadline"], reads=reads, catalog=catalog)
            except CardStorageError as exc:
                if str(exc) == "card_read_collection_conflict":
                    raise _Refused("card_read_collection_conflict", 409) from None
                raise
            ref = validate_read_collection_ref({
                "schema": READ_COLLECTION_REF_SCHEMA, "collection_id": collection_id, "scope": header["scope"],
                "count": header["count"], "root": header["root"], "catalog": header["catalog"],
                "deadline": header["deadline"]})
            result = {"kind": "collection", "ref": ref}
        except _Refused as exc:
            result = {"kind": "refused", "code": exc.code, "status": exc.status}
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
        return self._collection_signed(caller, data, digest, result)

    async def _reads(self, scope: str, persons: list[str]) -> list[dict[str, Any]]:
        """Every person's descriptors through the census identity path, or a whole-registration refusal."""
        reads: dict[tuple[str, str], dict[str, Any]] = {}

        def add(entry: Mapping[str, Any]) -> None:
            if entry.get("state") == "in_transaction":
                raise _Refused("card_read_collection_in_transaction", 409)
            if entry.get("state") not in ("present", "absent"):
                raise _Refused("card_read_collection_entry_invalid", 409)
            key = (entry["subject_hash"], entry["access_id"])
            read = {"subject_hash": key[0], "access_id": key[1], "revision": entry.get("revision", 0)}
            if reads.setdefault(key, read) != read:
                raise _Refused("card_read_collection_entry_invalid", 409)

        for person in persons:
            result = await self._person(scope, person)
            add(result["my"])
            add(result["control"])
            chain = result["chain"]
            if chain["state"] == "in_transaction":
                raise _Refused("card_read_collection_in_transaction", 409)
            if chain["state"] == "unavailable":
                raise _Refused("card_read_collection_chain_unavailable", 503)
            if chain["state"] == "invalid":
                raise _Refused("card_read_collection_chain_invalid", 409)
            for card in chain["cards"]:
                add(card)
        if len(reads) > MAX_COLLECTION_READS:
            raise _Refused("card_read_collection_too_large", 413)
        return [reads[key] for key in sorted(reads)]

    async def _catalog_digest(self) -> str:
        if self._catalog is None:
            raise _Refused("card_catalog_reservation_unavailable", 503)
        active = await self._catalog.read_active()
        return "" if active is None else catalog_version_digest(active.version, active.content_hash)

    def _collection_signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                           result: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_echo": data["request_echo"], "request_digest": digest, "scope": data["scope"],
                    "persons": list(data["persons"]), "actor_subject": data["actor_subject"],
                    "request_id": data["request_id"], "deadline": data["deadline"], "result": dict(result)}
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.COLLECTION)
        return {"ok": unsigned["result"].get("kind") != "refused",
                "collection_answer": {**unsigned, "receipt_proof": proof}}


__all__ = ["ANSWER_SCHEMA", "CardReadCollectionOperation", "OPERATION", "REQUEST_FIELDS", "REQUEST_SCHEMA",
           "collection_id_for", "collection_request_digest"]

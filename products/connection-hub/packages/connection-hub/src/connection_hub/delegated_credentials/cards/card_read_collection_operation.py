"""W502 lane D: ``card_read_collection_register``: seal a project-wide Card read collection on the Hub.

The initiator (Problem Board) names the scope and the persons from its own
fenced membership, exactly as for ``card_census_read``, plus the actor, its
request id and the request's absolute deadline. The Hub derives each person's
dependencies through the SAME qualified identity path as the census (My Card,
project Control, the complete upstream chain) and seals them once, as its own
collection (``card_read_collection.seal_collection``). The signed answer carries
only the bounded reference the initiator's decision intent stores.

The descriptors equal the initiator's ``read_reservations`` over the same
census: per person My and Control (revision, or 0 when absent) and every Card
of a ``complete`` chain, each distinct (subject_hash, access_id) once. A person
whose edge or chain is invalid contributes My and Control only, as the census
consumer does. A Card still in another decision, or a chain that is staged or
unavailable, refuses the WHOLE registration by name (retry after the decision);
no partial collection is ever sealed.

Admission is the census's: the caller descriptor, ``census_scope_prefix``, a
durable single-use nonce, and the request frozen before any await. Completeness
of the person list stays the caller's, as for the census.
"""

from __future__ import annotations

import contextlib
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
from .card_read_set import READ_COLLECTION_REF_SCHEMA
from .census_read import (
    MAX_PERSONS, PROOF_FIELDS, CardCensusReadOperation, _bounded, _Refused, _unsigned_refusal,
)
from .participant_operation import MAX_SKEW_SECONDS
from .store import CardStorageError

OPERATION = "card_read_collection_register"
REQUEST_SCHEMA = "card-read-collection-register.v1"
ANSWER_SCHEMA = "card-read-collection-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "request_echo", "scope", "persons", "exclude", "actor_subject", "request_id", "deadline")
# A write group's own targets, which are its writes and never its read dependencies (W651, claude-ops@spark1 23:23Z).
MAX_EXCLUDE = 8
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")


def collection_request_digest(request: Mapping[str, Any]) -> str:
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS}, contract=AnswerContract.COLLECTION)


def collection_id_for(*, service_id: str, scope: str, request_id: str, deadline: int) -> str:
    """Deterministic per caller, scope, request and deadline: a retry reaches the same sealed collection.

    The deadline is part of the id so a collection retention deleted (only after its deadline) can never
    be sealed again under the same id: registration refuses that deadline (``card_read_collection_expired``)."""
    return sha256_hex(canonical_json_bytes({"service_id": service_id, "scope": scope, "request_id": request_id,
                                            "deadline": deadline}))[:32]


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
            and persons == sorted(set(persons))
            and _valid_exclude(data["exclude"]))


def _valid_exclude(exclude: Any) -> bool:
    if type(exclude) is not list or len(exclude) > MAX_EXCLUDE:
        return False
    keys = []
    for item in exclude:
        if (not isinstance(item, Mapping) or set(item) != {"subject_hash", "access_id"}
                or type(item["subject_hash"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", item["subject_hash"])
                or not _bounded(item["access_id"])):
            return False
        keys.append((item["subject_hash"], item["access_id"]))
    return keys == sorted(set(keys))


def descriptors_of(person_results: list[Mapping[str, Any]], exclude: Any = ()) -> list[dict[str, Any]]:
    """The census consumer's read reservations over these person entries, minus ``exclude`` (a write
    group's own targets), or a named refusal."""
    reads: dict[tuple[str, str], dict[str, Any]] = {}
    for result in person_results:
        entries = [result["my"], result["control"]]
        chain = result["chain"]
        if any(entry["state"] == "in_transaction" for entry in entries) or chain["state"] == "in_transaction":
            raise _Refused("card_read_collection_in_transaction", 409)
        if chain["state"] == "unavailable":
            raise _Refused("card_read_collection_unavailable", 503)
        if chain["state"] == "complete":
            entries += list(chain["cards"])
        for entry in entries:
            key = (entry["subject_hash"], entry["access_id"])
            reads[key] = {"subject_hash": key[0], "access_id": key[1],
                          "revision": entry["revision"] if entry["state"] == "present" else 0}
    for item in exclude or ():
        reads.pop((item["subject_hash"], item["access_id"]), None)  # a newly minted target need not be read
    if not reads:
        raise _Refused("card_read_collection_empty", 409)
    if len(reads) > MAX_COLLECTION_READS:
        raise _Refused("card_read_collection_too_large", 413)
    return [reads[key] for key in sorted(reads)]


class CardReadCollectionOperation(CardCensusReadOperation):
    """``answer(data)`` for ``card_read_collection_register``; bound by the composition root.

    ``card_store`` is the Hub's Card store, which also holds the sealed collections.
    """

    def __init__(self, *, nonce_prefix: str = "connection-hub:card-collection:nonce:",
                 collection_lock: Any = None, **kwargs: Any) -> None:
        """``collection_lock(collection_id)`` is the Card service's collection lock (the same one every
        stage, finish and the retention sweep take); production always passes it."""
        super().__init__(nonce_prefix=nonce_prefix, **kwargs)
        self._collection_lock = collection_lock

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
                raise _Refused("card_read_collection_expired", 409)
            reads = descriptors_of([await self._person(data["scope"], person) for person in data["persons"]],
                                   data["exclude"])
            catalog = await self._active_catalog()
            if catalog is None:
                raise _Refused("card_catalog_reservation_unavailable", 503)
            catalog_digest = catalog_version_digest(catalog["version"], catalog["content_hash"])
            collection_id = collection_id_for(service_id=caller.service_id, scope=data["scope"],
                                              request_id=data["request_id"], deadline=data["deadline"])
            try:
                async with (self._collection_lock(collection_id) if self._collection_lock is not None
                            else contextlib.nullcontext()):
                    # Re-checked under the lock, after every await: a registration that began before the
                    # deadline never seals after it, so retention's deleted id is never sealed again.
                    if data["deadline"] <= int(self._clock()):
                        raise _Refused("card_read_collection_expired", 409)
                    header = await seal_collection(
                        self._cards, collection_id=collection_id, scope=data["scope"],
                        actor_subject=data["actor_subject"], request_id=data["request_id"],
                        deadline=data["deadline"], reads=reads, catalog=catalog_digest)
            except CardStorageError as exc:
                if str(exc) == "card_read_collection_conflict":
                    # The same request sealed other reads before: re-register under a new request id.
                    raise _Refused("card_read_collection_conflict", 409) from None
                raise
            result = {"kind": "collection", "ref": {"schema": READ_COLLECTION_REF_SCHEMA, **{
                key: header[key] for key in ("collection_id", "scope", "count", "root", "catalog", "deadline")}}}
        except _Refused as exc:
            result = {"kind": "refused", "code": exc.code, "status": exc.status}
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
        return self._signed_collection(caller, data, digest, result)

    def _signed_collection(self, caller: Any, data: Mapping[str, Any], digest: str,
                           result: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_echo": data["request_echo"], "request_digest": digest,
                    **{name: data[name] for name in REQUEST_FIELDS if name not in ("schema", "request_echo")},
                    "result": dict(result)}
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.COLLECTION)
        return {"ok": result.get("kind") == "collection", "collection_answer": {**unsigned, "receipt_proof": proof}}


__all__ = ["ANSWER_SCHEMA", "CardReadCollectionOperation", "OPERATION", "REQUEST_FIELDS", "REQUEST_SCHEMA",
           "collection_id_for", "collection_request_digest", "descriptors_of"]

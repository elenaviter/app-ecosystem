"""W502: a signed, optimistic read of one project's person Cards and the active catalog.

An initiator such as Problem Board computes its business check (the last
usable administrator) over the exact Control and My Card revisions and the
active catalog, then names them as read reservations in its intent
(``card:``, ``card-absent:``, ``catalog-active:``). This operation gives it
those inputs, server to server (CodeApp 20:17):

- authenticated exactly like ``card_transaction_participant``: the caller is
  the admission proof's service id from the same caller descriptor, with a
  durable single-use nonce, and the request is frozen before any await;
- the caller may read only scopes its descriptor entitles it to
  (``census_scope_prefix``; none, no census), refused signed
  ``card_census_scope_forbidden`` otherwise;
- the caller names the scope and the persons; the Hub DERIVES each person's
  My Card and project Control ids from them, so only that project's person
  Cards are readable, then follows the QUALIFIED identity path: the My Card's
  edge identity from its raw provenance (``ProjectPersonCardIdentity``), edge
  validation against the current Control, and the complete upstream Control
  chain through ``compose_control_hierarchy`` (W579: upstream P op_P (C op_C
  My)). Every ancestor comes from stored bindings, never a caller-named id;
- the answer is signed (``card-census-answer.v1``) with the same construction
  as the shared participant answers, over every echoed request field.

The read takes no lock and authorizes nothing: Hub prepare re-verifies every
named revision and the catalog version, and a change in between is refused by
name (``card_dependency_moved``, ``catalog_version_moved``,
``catalog_publication_pending``) for the caller to reread and retry.

COMPLETENESS IS THE CALLER'S (EMain 20:19). The Hub answers only for the
persons the caller names; it cannot know who belongs to the project. An
answer is never a census by itself: the initiator must take the person list
from its own fenced membership census inside the same witnessed transaction,
and a person missing from that list must make its check fail, never pass.

Each present Card carries only the fields a capability evaluation reads
(``CAPABILITY_FIELDS``), not the whole record. An answer larger than the
consumer's bound is refused (``card_census_too_large``) rather than signed:
the caller pages its persons, and may read the catalog alone (``persons``
empty, ``include_catalog`` true).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex
from service_foundation.coordination.participant_answer import AnswerContract
from service_foundation.coordination.participant_answer import request_digest as shared_request_digest
from service_foundation.coordination.participant_answer import sign_participant_answer

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from ..controls.project_person import (
    PROJECT_PERSON_CONTROL_PROPERTY, ProjectPersonControlError, ProjectPersonControlIdentity,
)
from ..controls.hierarchy import compose_control_hierarchy
from ..controls.effective import ControlCardMismatch
from ..card_property_classes import PERSONAL, authorization_properties
from ..project_identity_lifecycle import (
    PROJECT_IDENTITY_EDGE_PROVENANCE, ProjectIdentityLifecycleError, ProjectPersonCardIdentity,
)
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller
from .store import CardStorageError, subject_hash_for

OPERATION = "card_census_read"
REQUEST_SCHEMA = "card-census-read.v1"
ANSWER_SCHEMA = "card-census-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "request_echo", "scope", "persons", "include_catalog")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
MAX_PERSONS = 500
# Problem Board's consumer refuses an answer over 512 KiB (EMain 20:19); keep
# headroom for the proof and transport wrapper.
MAX_ANSWER_BYTES = 512 * 1024 - 4096
CAPABILITY_FIELDS = (
    "schema", "access_id", "client_id", "grantor_subject", "delegate_subject", "issuer_kind", "issuer_ref",
    "card_revision", "card_kind", "source", "state", "catalog_version",
    "expires_at", "composition_mode", "account_scope", "identity_scope", "operations", "resource_grants",
    "resource_operations", "resource_acceptance", "named_service_operations", "named_services", "control_card",
)
# Every CLASSIFIED authorization property travels (CodeApp 20:38, EMain #618):
# card_property_classes decides each key; personal settings and any key not
# yet classified are withheld (fail closed). The size refusal bounds the answer.
PERSONAL_PROPERTIES = tuple(sorted(PERSONAL))
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_BOUNDED = 256


def census_request_digest(request: Mapping[str, Any]) -> str:
    """The shared helper's digest under the closed census contract (AE #619)."""
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS}, contract=AnswerContract.CENSUS)


def answer_frame_signature(unsigned: Mapping[str, Any], *, schema: str, secret: str | bytes, signer_id: str,
                           timestamp: str) -> str:
    """The shared participant-answer construction (AE #608): HMAC-SHA256 over the newline join.

    #608's helper fixes the participant's field sets, so this answer signs with
    the same construction here; a test pins it to the helper's bytes (EMain #616).
    """
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    frame = "\n".join((schema, signer_id, timestamp, unsigned["request_echo"],
                       sha256_hex(canonical_json_bytes(dict(unsigned))))).encode("utf-8")
    return base64.urlsafe_b64encode(hmac.new(key, frame, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def census_answer_signature(unsigned: Mapping[str, Any], *, secret: str | bytes, signer_id: str,
                            timestamp: str) -> str:
    """The shared helper's signature under the closed census contract (AE #619)."""
    return sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=secret, signer_id=signer_id,
                                   timestamp=timestamp, contract=AnswerContract.CENSUS)["signature"]


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable()


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof, persons = data["service_proof"], data["persons"]
    return (isinstance(proof, Mapping) and set(proof) == PROOF_FIELDS
            and all(type(proof[name]) is str for name in PROOF_FIELDS)
            and data["schema"] == REQUEST_SCHEMA and type(data["request_echo"]) is str
            and bool(_ECHO.fullmatch(data["request_echo"])) and _bounded(data["scope"])
            and type(data["include_catalog"]) is bool
            and type(persons) is list and len(persons) <= MAX_PERSONS
            and (bool(persons) or data["include_catalog"] is True)
            and all(_bounded(person) for person in persons)
            and persons == sorted(set(persons)))  # sorted and unique: one canonical request


def _unsigned_refusal(code: str, status: int) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code}}


class CardCensusReadOperation:
    """``answer(data)`` for ``card_census_read``; bound by the composition root."""

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], card_store: Any, catalog_store: Any,
                 nonces: Any, clock: Callable[[], float],
                 nonce_prefix: str = "connection-hub:card-census:nonce:") -> None:
        self._callers = dict(callers)
        self._cards = card_store
        self._catalog = catalog_store
        self._nonces = nonces
        self._clock = clock
        self._nonce_prefix = nonce_prefix

    async def answer(self, data: Any) -> dict[str, Any]:
        try:  # frozen before any await (CodeApp 19:35)
            data = json.loads(json.dumps(dict(data), allow_nan=False)) if isinstance(data, Mapping) else None
        except (TypeError, ValueError):
            data = None
        if not _valid_request(data):
            return _unsigned_refusal("card_census_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = census_request_digest(data)
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
                # Only scopes the caller's descriptor entitles it to read (EMain #616).
                raise _Refused("card_census_scope_forbidden", 403)
            result = {"kind": "census", "persons": [await self._person(data["scope"], person)
                                                    for person in data["persons"]],
                      "catalog": await self._active_catalog() if data["include_catalog"] else None}
        except _Refused as exc:
            result = {"kind": "refused", "code": exc.code, "status": exc.status}
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
        return self._signed(caller, data, digest, result)

    async def _person(self, scope: str, person: str) -> dict[str, Any]:
        try:
            expected = ProjectPersonCardIdentity.build(project_ref=scope, person_subject=person)
            ProjectPersonControlIdentity.build(project_ref=scope, target_subject=person)
        except (ProjectIdentityLifecycleError, ProjectPersonControlError):
            raise _Refused("card_census_person_invalid", 400) from None
        my_entry, my = await self._read(subject_hash_for(person), expected.my_card_id, my_card=True)
        control_entry, control = await self._read(subject_hash_for(expected.project_subject), expected.control_id)
        result = {"person": person, "my": my_entry, "control": control_entry,
                  "edge": {"state": "missing"}, "chain": {"state": "missing", "cards": []}}
        if my is None:
            return result
        try:
            identity = ProjectPersonCardIdentity.from_my_card(my)
        except (ProjectIdentityLifecycleError, ProjectPersonControlError, ValueError) as exc:
            result["edge"] = {"state": "invalid", "reason": getattr(exc, "reason", "project_identity_edge_invalid")}
            return result
        if identity.edge_ref != expected.edge_ref:
            result["edge"] = {"state": "invalid", "reason": "project_identity_edge_conflict"}
            return result
        reason = identity.edge(control_card=control, my_card=my).validation_reason()
        result["edge"] = {"state": "invalid", "reason": reason} if reason else {"state": "valid"}
        if reason or control is None or my.control_card is None:
            return result
        result["chain"] = await self._chain(my)
        return result

    async def _chain(self, my: Any) -> dict[str, Any]:
        """The complete upstream chain from stored bindings: the direct Control first, then each ancestor."""
        staged: list[str] = []

        async def load_control(access_id: str, *, grantor_subject: str):
            entry, authority = await self._read(subject_hash_for(grantor_subject), access_id)
            if entry["state"] == "in_transaction":
                staged.append(access_id)
            return authority

        try:
            hierarchy = await compose_control_hierarchy(my, load_control=load_control)
        except ControlCardMismatch as exc:
            if staged:
                return {"state": "in_transaction", "cards": []}  # an ancestor is staged: reread after its decision
            return {"state": "unavailable", "reason": exc.reason, "cards": []}
        return {"state": "complete", "cards": [_present(authority) for authority in hierarchy.dependencies]}

    async def _read(self, subject_hash: str, access_id: str, *, my_card: bool = False):
        base = {"subject_hash": subject_hash, "access_id": access_id}
        try:
            loaded = await self._cards.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        except CardStorageError as exc:
            if str(exc) in ("card_transaction_undecided", "card_effects_pending"):
                return {**base, "state": "in_transaction"}, None  # staged: reread after it is decided
            raise
        if loaded is None:
            return {**base, "state": "absent"}, None
        return _present(loaded[1], my_card=my_card), loaded[1]

    async def _active_catalog(self) -> dict[str, Any] | None:
        if self._catalog is None:
            raise _Refused("card_catalog_reservation_unavailable", 503)
        active = await self._catalog.read_active()
        if active is None:
            return None
        return {"version": active.version, "content_hash": active.content_hash, "document": active.to_dict()}

    def _signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                result: Mapping[str, Any]) -> dict[str, Any]:
        def unsigned_for(value: Mapping[str, Any]) -> dict[str, Any]:
            return {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_echo": data["request_echo"], "request_digest": digest, "scope": data["scope"],
                    "persons": list(data["persons"]), "include_catalog": data["include_catalog"],
                    "result": dict(value)}

        unsigned = unsigned_for(result)
        if len(canonical_json_bytes(unsigned)) > MAX_ANSWER_BYTES:
            unsigned = unsigned_for({"kind": "refused", "code": "card_census_too_large", "status": 413})
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.CENSUS)
        return {"ok": unsigned["result"].get("kind") != "refused",
                "census_answer": {**unsigned, "receipt_proof": proof}}


def _present(authority: Any, *, my_card: bool = False) -> dict[str, Any]:
    """The fields a qualified identity and capability evaluation reads, so the caller can rebuild the Card
    with ``CardAuthority.from_mapping`` and run it (CodeApp 20:34/20:38); person-owned settings stay out."""
    raw = authority.to_dict()
    fields = {name: raw[name] for name in CAPABILITY_FIELDS if name in raw}
    properties = dict(authority.properties or {})
    fields["properties"] = authorization_properties(properties)
    edge = dict(authority.provenance or {}).get(PROJECT_IDENTITY_EDGE_PROVENANCE)
    fields["provenance"] = {PROJECT_IDENTITY_EDGE_PROVENANCE: edge} if edge is not None else {}
    return {"subject_hash": subject_hash_for(authority.grantor_subject), "access_id": authority.access_id,
            "state": "present", "revision": authority.card_revision, "authority": fields}


class _Refused(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


__all__ = ["ANSWER_SCHEMA", "answer_frame_signature", "CAPABILITY_FIELDS", "CardCensusReadOperation", "MAX_ANSWER_BYTES", "MAX_PERSONS",
           "OPERATION", "REQUEST_FIELDS",
           "REQUEST_SCHEMA", "census_answer_signature", "census_request_digest"]

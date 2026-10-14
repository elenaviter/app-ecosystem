"""``card_invitation_census_read``: one managed project's invitation Controls, enumerated by the Hub (W814).

A project host (Problem Board) deleting a project must hold every pending invitation Control at its exact
revision. The invitee-session read (``project_invitation_pending_revision``) cannot serve the deleting admin,
and the person census derives only named persons' Cards. This read lets the HUB enumerate the set itself:

- the caller names only the scope (no list): the Hub derives the project's partition
  (``project_authority_subject(scope)``), lists it, and keeps exactly the Cards whose issuer kind is
  ``project-invitation`` and whose identity marker names this project; another project's Card cannot appear;
- every Card is read through the decision-consulting read: one staged or effects-pending invitation Card
  refuses the WHOLE read (``card_invitation_census_in_transaction``), never a partial set;
- an invitation-issuer Card whose identity does not verify for this project refuses the whole read
  (``card_invitation_census_identity_invalid``), never skipped;
- the answer lists ``{access_id, invitation_ref, revision, state}`` sorted by ``access_id`` (state ``pending``,
  ``withdrawn`` or ``redeemed``), with the partition's ``subject_hash``, the ``count`` and a ``root`` digest of
  the canonical entries, signed under ``AnswerContract.INVITATION_CENSUS``.

This proves the set as of the read. Stability until the deletion decision is the caller's composition: the
pending entries join its held read set at their exact revisions, a NEW invitation needs the host's own guarded
invite save (fenced by its deletion lock), and direct Hub writes to managed Cards are refused (W578). The Hub
has no "new child" fence, so a future invitation id is not claimed held here.

Authentication, single-use nonce, the scope entitlement (``census_scope_prefix``) and signed refusals are the
census read's (census_read.py); nothing about who may invite or how an invitation is redeemed changes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Mapping

from service_foundation.coordination.durable_wire import WireRefused, canonical_json_bytes, sha256_hex
from service_foundation.coordination.participant_answer import AnswerContract
from service_foundation.coordination.participant_answer import request_digest as shared_request_digest
from service_foundation.coordination.participant_answer import sign_participant_answer

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from ..controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlError, ProjectInvitationControlIdentity,
)
from ..controls.project_person import project_authority_subject
from ..project_invitation_claim import PROJECT_INVITATION_BINDING_PROVENANCE
from .model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller
from .store import CardStorageError, subject_hash_for

OPERATION = "card_invitation_census_read"
REQUEST_SCHEMA = "card-invitation-census-read.v1"
ANSWER_SCHEMA = "card-invitation-census-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "request_echo", "scope")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
# The same consumer bound as the person census.
MAX_ANSWER_BYTES = 512 * 1024 - 4096
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_BOUNDED = 256


def invitation_census_request_digest(request: Mapping[str, Any]) -> str:
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS},
                                 contract=AnswerContract.INVITATION_CENSUS)


def invitation_entries_root(entries: list[Mapping[str, Any]]) -> str:
    """The digest a consumer binds: the canonical sorted entries, exactly as answered."""
    return sha256_hex(canonical_json_bytes([dict(entry) for entry in entries]))


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable()


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof = data["service_proof"]
    return (isinstance(proof, Mapping) and set(proof) == PROOF_FIELDS
            and all(type(proof[name]) is str for name in PROOF_FIELDS)
            and data["schema"] == REQUEST_SCHEMA and type(data["request_echo"]) is str
            and bool(_ECHO.fullmatch(data["request_echo"])) and _bounded(data["scope"]))


def _unsigned_refusal(code: str, status: int) -> dict[str, Any]:
    _LOG.warning("card invitation census refused unsigned: code=%s status=%s", code, status)
    return {"ok": False, "status": status, "error": {"code": code}}


class _Refused(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def _state(authority: Any) -> str:
    if authority.state == CARD_STATE_ACTIVE:
        return "pending"
    if authority.state == CARD_STATE_REVOKED:
        bound = dict(authority.provenance or {}).get(PROJECT_INVITATION_BINDING_PROVENANCE) is not None
        return "redeemed" if bound else "withdrawn"
    raise _Refused("card_invitation_census_identity_invalid", 409)


class CardInvitationCensusReadOperation:
    """``answer(data)`` for ``card_invitation_census_read``; bound by the composition root."""

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], card_store: Any, nonces: Any,
                 clock: Callable[[], float],
                 nonce_prefix: str = "connection-hub:card-invitation-census:nonce:") -> None:
        self._callers = dict(callers)
        self._cards = card_store
        self._nonces = nonces
        self._clock = clock
        self._nonce_prefix = nonce_prefix

    async def answer(self, data: Any) -> dict[str, Any]:
        try:  # frozen before any await
            data = json.loads(json.dumps(dict(data), allow_nan=False)) if isinstance(data, Mapping) else None
        except (TypeError, ValueError):
            data = None
        if not _valid_request(data):
            return _unsigned_refusal("card_invitation_census_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = invitation_census_request_digest(data)
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
                raise _Refused("card_invitation_census_scope_forbidden", 403)
            result = await self._census(data["scope"])
        except _Refused as exc:
            result = {"kind": "refused", "code": exc.code, "status": exc.status}
        except Exception as exc:  # noqa: BLE001 - authenticated, so signed; never internal text
            _LOG.warning("card invitation census failed: %s", type(exc).__name__)
            result = {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
        return self._signed(caller, data, digest, result)

    async def _census(self, scope: str) -> dict[str, Any]:
        subject_hash = subject_hash_for(project_authority_subject(scope))
        entries: list[dict[str, Any]] = []
        for access_id in sorted(await self._cards.list_card_ids(subject_hash=subject_hash)):
            try:
                loaded = await self._cards.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            except CardStorageError as exc:
                if str(exc) in ("card_transaction_undecided", "card_effects_pending"):
                    # Which Card it is does not matter to the answer: the whole set is unproven until decided.
                    raise _Refused("card_invitation_census_in_transaction", 409) from None
                raise
            if loaded is None:
                continue  # listed then absent: not a current Card of this partition
            authority = loaded[1]
            if authority.issuer_kind != PROJECT_INVITATION_CONTROL_ISSUER_KIND:
                continue  # the partition also holds the project's person Controls
            try:
                identity = ProjectInvitationControlIdentity.from_authority(authority)
            except ProjectInvitationControlError:
                raise _Refused("card_invitation_census_identity_invalid", 409) from None
            if identity.project_ref != scope or identity.control_id != authority.access_id:
                raise _Refused("card_invitation_census_identity_invalid", 409)
            entries.append({"access_id": authority.access_id, "invitation_ref": identity.invitation_ref,
                            "revision": authority.card_revision, "state": _state(authority)})
        return {"kind": "invitation_census", "subject_hash": subject_hash, "invitations": entries,
                "count": len(entries), "root": invitation_entries_root(entries)}

    @staticmethod
    def _unsigned(caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                  value: Mapping[str, Any]) -> dict[str, Any]:
        return {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                "request_echo": data["request_echo"], "request_digest": digest, "scope": data["scope"],
                "result": dict(value)}

    def _signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                result: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = self._unsigned(caller, data, digest, result)
        try:
            encoded = canonical_json_bytes(unsigned)
        except WireRefused:
            _LOG.error("card invitation census answer not signable")
            raise
        if len(encoded) > MAX_ANSWER_BYTES:
            unsigned = self._unsigned(caller, data, digest,
                                      {"kind": "refused", "code": "card_invitation_census_too_large", "status": 413})
        outcome = unsigned["result"]
        if outcome.get("kind") == "refused":
            _LOG.warning("card invitation census refused: code=%s status=%s", outcome.get("code"), outcome.get("status"))
        else:
            _LOG.info("card invitation census answered: %d invitation(s), %d bytes", outcome["count"], len(encoded))
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.INVITATION_CENSUS)
        return {"ok": outcome.get("kind") != "refused", "invitation_census_answer": {**unsigned, "receipt_proof": proof}}


_LOG = logging.getLogger(__name__)

__all__ = ["ANSWER_SCHEMA", "CardInvitationCensusReadOperation", "MAX_ANSWER_BYTES", "OPERATION", "REQUEST_FIELDS",
           "REQUEST_SCHEMA", "invitation_census_request_digest", "invitation_entries_root"]

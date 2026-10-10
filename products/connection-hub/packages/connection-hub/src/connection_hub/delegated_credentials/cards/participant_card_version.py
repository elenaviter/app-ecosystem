"""W661 piece 2: ``card_version``, Problem Board's STAGE / PUBLISH / ROLLBACK of one Card save.

PB calls the Hub; the Hub never calls PB during a save (design v2, contract
v6.2). It replaces, for PB's save path, the Hub-to-PB ``plan-authorize`` and
``transaction_authority`` pulls and the Hub's intent copy of the Card:

- authenticates exactly like ``card_lifecycle_plan``: the caller is the
  admission proof's service id, the proof covers the digest of every request
  field, the nonce is durable and single-use, and the request is frozen
  before any await; a caller saves only in scopes its descriptor entitles it
  to plan in (``plan_scope_prefix``);
- STAGE: PB authorized the save by role, so the request carries PB's
  ``delegable_grants`` (and the project Control locator), and the Hub turns
  them into one allow per step for the generic planner (W594). The planner
  builds each new version from its base (``original_revision`` is the
  contract's ``base_version``); piece 1's store then writes it, not yet final,
  under the Card's lock. The answer is links only: ``{card, version, checksum}``;
- PUBLISH and ROLLBACK carry the ``txn`` alone: piece 1's marker names the
  members, so nothing is listed and no Card value travels back;
- Hub effects (section 4a): only ``handle_binding`` of an edited agent Card
  exists on this path. Its identity is ``(txn, access_id, kind, key)`` plus the
  member's version links in the marker; the store records each named result.

Every authenticated answer is signed (``card-version-answer.v1``, the shared
participant answer under ``AnswerContract.CARD_VERSION``), refusals included.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex
from service_foundation.coordination.participant_answer import AnswerContract
from service_foundation.coordination.participant_answer import request_digest as shared_request_digest
from service_foundation.coordination.participant_answer import sign_participant_answer

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from ..catalog.reservations import catalog_version_digest
from ..durable_io import cancellation_safe_await
from ..project_authorization import (
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, ProjectAuthorizationDecision,
    ProjectAuthorizationError, ProjectControlLocator,
)
from .effect_targets import CREDENTIAL_ISSUE_SUPERSEDED
from .lifecycle_plan_operation import MAX_REQUEST_BYTES, MAX_STEPS, _Refused as _StepsRefused, plan_steps
from .model import CARD_STATE_ACTIVE, CardAuthority
from .participant_effects import ParticipantEffectRefused
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller

OPERATION = "card_version"
REQUEST_SCHEMA = "card-version-request.v1"
ANSWER_SCHEMA = "card-version-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "op", "request_echo", "scope", "txn", "request_id", "at", "catalog", "actor_subject",
                  "actor_kind", "delegable_grants", "project_control", "creations", "updates", "links")
STAGE_FIELDS = ("request_id", "at", "catalog", "actor_subject", "actor_kind", "delegable_grants", "project_control",
                "creations", "updates")
# The answer echoes only the call's identity (AnswerContract.CARD_VERSION); request_digest binds the rest.
ECHO_FIELDS = ("op", "request_echo", "scope", "txn")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
OPS = frozenset({"stage", "publish", "rollback"})
MAX_GRANTS = 256
_TXN = re.compile(r"[a-z0-9-]{32,128}\Z")
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BOUNDED = 256

# The contract's codes (section 3-5). A planner refusal that means "the base is
# not what PB authorized against" is card_changed; any other is edit_invalid.
REFUSALS = {"txn_closed": 409, "txn_unknown": 404, "card_changed": 409, "stage_catalog_moved": 409,
            "edit_invalid": 422, "effects_pending": 503, "storage_unavailable": 503,
            "request_scope_invalid": 403, "stage_txn_conflict": 409, "txn_not_staged": 409}
# Store codes PB sees under the contract's name: a txn staged under another scope or caller is out of scope.
_ALIASES = {"txn_scope_mismatch": "request_scope_invalid"}
_CARD_CHANGED = frozenset({"card_plan_original_revision_changed", "card_plan_update_target_absent",
                           "card_plan_target_exists", "card_plan_revision_invalid",
                           "card_effect_target_revision_moved", "card_plan_reset_control_moved",
                           "card_plan_reset_display_moved"})
_UNAVAILABLE = frozenset({"delegated_catalog_unavailable", "delegated_cards_unavailable"})
HANDLE_BINDING = "handle_binding"

Planner = Callable[..., Awaitable[Mapping[str, Any]]]


class CardVersionRefused(Exception):
    """A named contract refusal, raised by this handler or by piece 1's store."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        code = _ALIASES.get(code, code)
        self.code = code if code in REFUSALS else "storage_unavailable"


class CardVersionStore(Protocol):
    """Piece 1's (Ops) store, as piece 2 calls it. Every call runs under the members' Card locks.

    ``effects`` are opaque to the store. STAGE writes the marker ``staging``
    first, naming the members and the effects (Infra K4), and only then calls
    ``prepare``, writes the not-yet-final bodies and sets ``staged``. So
    nothing ``prepare`` writes (an agent's re-wrap) exists without a marker
    that names it. The store hands each effect without a recorded result to
    ``apply`` (PUBLISH, or ROLLBACK finding ``published``) and records the
    named result. ROLLBACK of ``staging`` or ``staged`` hands every effect to
    ``release``, then deletes the bodies and the marker. Every mutating path
    runs drained, under the Card locks. ``scope`` and ``caller`` are the
    authenticated binding: STAGE records them in the marker, and a replay,
    PUBLISH or ROLLBACK that names another refuses ``txn_scope_mismatch``.
    ROLLBACK also gets STAGE's answer ``links`` and STAGE's ``at``: a
    completed PUBLISH removes the marker, and the version file they name
    (carrying this txn) then answers ``already_published`` (EMain D2).
    """

    async def stage(self, txn: str, *, request_id: str, request_digest: str, catalog: Mapping[str, Any],
                    actor_subject: str, actor_kind: str, members: Sequence[Mapping[str, Any]],
                    effects: Sequence[Mapping[str, Any]], prepare: Callable[[], Awaitable[None]],
                    at: datetime, scope: str, caller: str) -> Mapping[str, Any]: ...

    async def publish(self, txn: str, *, scope: str, caller: str,
                      apply: Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[str]]) -> Mapping[str, Any]: ...

    async def rollback(self, txn: str, *, scope: str, caller: str, links: Sequence[Mapping[str, Any]],
                       at: datetime, apply: Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[str]],
                       release: Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[None]]
                       ) -> Mapping[str, Any]: ...

    async def read_current(self, subject_hash: str, access_id: str) -> Mapping[str, Any] | None: ...


def card_version_request_digest(request: Mapping[str, Any]) -> str:
    """The shared helper's digest of exactly the unsigned request fields, under the closed CARD_VERSION contract."""
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS},
                                 contract=AnswerContract.CARD_VERSION)


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable()


def _at(value: Any) -> datetime | None:
    """STAGE's ``at``: the request's own time, ISO 8601 with an offset (EMain 16:41Z). A retry sends the same."""
    if not _bounded(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def stage_digest(data: Mapping[str, Any]) -> str:
    """The STAGE content binding: every request field but the per-call ``request_echo``.

    A retry of the same save (a fresh echo and nonce) has the same stage digest, so the store finds its own
    marker; a different edit under the same txn does not (stage_txn_conflict). The signed answer still
    binds the exact call through ``request_digest``.
    """
    return sha256_hex(canonical_json_bytes({name: data[name] for name in REQUEST_FIELDS
                                            if name not in ("schema", "op", "request_echo", "links")}))


def _valid_stage(data: Mapping[str, Any]) -> bool:
    catalog, grants, control = data["catalog"], data["delegable_grants"], data["project_control"]
    creations, updates = data["creations"], data["updates"]
    return (_bounded(data["request_id"]) and _at(data["at"]) is not None and _bounded(data["actor_subject"])
            and data["actor_subject"] == data["actor_subject"].strip()
            and data["actor_kind"] in ("caller", "grantor")
            and isinstance(catalog, Mapping) and set(catalog) == {"version", "content_hash"}
            and _bounded(catalog["version"]) and type(catalog["content_hash"]) is str
            and bool(_HASH.fullmatch(catalog["content_hash"]))
            and type(grants) is list and len(grants) <= MAX_GRANTS and all(_bounded(grant) for grant in grants)
            and (control is None or isinstance(control, Mapping) and set(control) == {"control_id", "holder_subject"}
                 and all(_bounded(control[name]) for name in control))
            and type(creations) is list and type(updates) is list
            and 1 <= len(creations) + len(updates) <= MAX_STEPS
            and all(isinstance(item, Mapping) for item in (*creations, *updates)))


def _valid_links(links: Any) -> bool:
    """ROLLBACK's links: exactly STAGE's answer, which PB keeps in its DECIDED row (EMain D2); [] when it has none."""
    return (type(links) is list and len(links) <= MAX_STEPS and all(
        isinstance(link, Mapping) and set(link) == {"card", "version", "checksum"}
        and isinstance(link["card"], Mapping) and set(link["card"]) == {"subject_hash", "access_id"}
        and _bounded(link["card"]["subject_hash"]) and _bounded(link["card"]["access_id"])
        and type(link["version"]) is int and link["version"] >= 1 and _bounded(link["checksum"])
        for link in links))


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof = data["service_proof"]
    if (not isinstance(proof, Mapping) or set(proof) != PROOF_FIELDS
            or any(type(proof[name]) is not str for name in PROOF_FIELDS)):
        return False
    if (data["schema"] != REQUEST_SCHEMA or data["op"] not in OPS or type(data["request_echo"]) is not str
            or not _ECHO.fullmatch(data["request_echo"]) or not _bounded(data["scope"])
            or type(data["txn"]) is not str or not _TXN.fullmatch(data["txn"])):
        return False
    if len(canonical_json_bytes({name: data[name] for name in REQUEST_FIELDS})) > MAX_REQUEST_BYTES:
        return False
    if data["op"] == "stage":
        return _valid_stage(data) and data["links"] is None
    # PUBLISH names the txn alone (its marker names the members). ROLLBACK also carries STAGE's answer
    # links, so it can find a version whose marker a completed PUBLISH already removed (EMain D2).
    if data["op"] == "publish":
        return data["links"] is None and all(data[name] is None for name in STAGE_FIELDS)
    # ROLLBACK also carries STAGE's `at`: the version file name derives from it (Ops, D2).
    return (_valid_links(data["links"]) and _at(data["at"]) is not None
            and all(data[name] is None for name in STAGE_FIELDS if name != "at"))


def _unsigned_refusal(code: str, status: int) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code}}


def _refusal(code: str) -> dict[str, Any]:
    code = code if code in REFUSALS else "storage_unavailable"
    return {"kind": "refused", "code": code, "status": REFUSALS[code]}


def _links(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The store's member links, and nothing else: no Card field leaves the Hub."""
    members = answer.get("members") if isinstance(answer, Mapping) else None
    if not isinstance(members, (list, tuple)):
        raise CardVersionRefused("storage_unavailable")
    links = []
    for member in members:
        card = member.get("card") if isinstance(member, Mapping) else None
        if (not isinstance(card, Mapping) or type(member.get("version")) is not int
                or type(member.get("checksum")) is not str):
            raise CardVersionRefused("storage_unavailable")
        links.append({"card": {"subject_hash": card["subject_hash"], "access_id": card["access_id"]},
                      "version": member["version"], "checksum": member["checksum"]})
    return links


def stage_authorization(data: Mapping[str, Any], digest: str) -> LifecyclePlanAuthorization:
    """PB's role decision as the planner's envelope: one allow per step, bounded by PB's delegable grants.

    PB authorized the save by the actor's committed role before STAGE
    (contract section 6, step 2); the request is authenticated, so the Hub
    asks nothing back. Each step keeps only those grants and the project
    Control locator PB named; nothing here widens them.
    """
    steps = plan_steps(list(data["creations"]), list(data["updates"]))
    request = LifecyclePlanAuthorizationRequest(
        actor_subject=data["actor_subject"], project_ref=data["scope"], request_id=data["request_id"],
        request_digest=digest, steps=steps)
    locator = ProjectControlLocator.from_mapping(data["project_control"])
    return LifecyclePlanAuthorization(request=request, decisions=tuple(
        (step.ref, ProjectAuthorizationDecision.allow(
            request.step_request(step), delegable_grants=tuple(data["delegable_grants"]), project_control=locator))
        for step in steps))


class CardVersionOperation:
    """``answer(data)`` for ``card_version``; bound by the composition root."""

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], store: CardVersionStore, planner: Planner,
                 host: Any, credential_handles: Any, nonces: Any, clock: Callable[[], float],
                 nonce_prefix: str = "connection-hub:card-version:nonce:") -> None:
        self._callers = dict(callers)
        self._store = store
        self._planner = planner
        self._host = host
        self._handles = credential_handles
        self._nonces = nonces
        self._clock = clock
        self._nonce_prefix = nonce_prefix

    async def answer(self, data: Any) -> dict[str, Any]:
        try:  # frozen before any await
            data = json.loads(json.dumps(dict(data), allow_nan=False)) if isinstance(data, Mapping) else None
        except (TypeError, ValueError):
            data = None
        try:
            valid = _valid_request(data)
        except Exception:  # noqa: BLE001 - a non-canonical request is invalid, by name
            valid = False
        if not valid:
            return _unsigned_refusal("card_version_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = card_version_request_digest(data)
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
            result = await self._dispatch(caller, data, digest)
        except CardVersionRefused as exc:
            result = _refusal(exc.code)
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = _refusal("storage_unavailable")
        return self._signed(caller, data, digest, result)

    async def _dispatch(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str) -> dict[str, Any]:
        prefix = caller.plan_scope_prefix
        if not prefix or not data["scope"].startswith(prefix):
            raise CardVersionRefused("request_scope_invalid")
        # The marker records the authenticated caller and scope at STAGE; a replay, PUBLISH or ROLLBACK
        # naming another binding refuses txn_scope_mismatch and touches nothing (Infra finding 2).
        txn, binding = data["txn"], {"scope": data["scope"], "caller": caller.service_id}
        if data["op"] == "stage":
            return {"kind": "staged", "members": await self._stage(data, binding)}
        if data["op"] == "publish":
            answer = await self._store.publish(txn, **binding, apply=self._apply)
            return {"kind": "published", "members": _links(answer)}
        answer = await self._store.rollback(txn, **binding, links=data["links"], at=_at(data["at"]),
                                            apply=self._apply, release=self._release)
        state = answer.get("state") if isinstance(answer, Mapping) else None
        if state not in ("rolled_back", "already_published", "unknown_txn"):
            raise CardVersionRefused("storage_unavailable")
        return {"kind": "rollback", "state": state}

    async def _stage(self, data: Mapping[str, Any], binding: Mapping[str, str]) -> list[dict[str, Any]]:
        txn, digest = data["txn"], stage_digest(data)
        try:
            authorization = stage_authorization(data, digest)
        except (ProjectAuthorizationError, _StepsRefused):
            raise CardVersionRefused("edit_invalid") from None
        planned = await self._planner(self._host, project_ref=data["scope"], creations=list(data["creations"]),
                                      updates=list(data["updates"]), actor_subject=data["actor_subject"],
                                      actor_kind=data["actor_kind"], request_id=data["request_id"],
                                      authorization=authorization)
        if not isinstance(planned, Mapping) or planned.get("ok") is not True:
            code = planned.get("error") if isinstance(planned, Mapping) else None
            if code in _CARD_CHANGED:
                raise CardVersionRefused("card_changed")
            raise CardVersionRefused("storage_unavailable" if code in _UNAVAILABLE else "edit_invalid")
        plan = planned["plan"]
        catalog = data["catalog"]
        if plan["catalog_digest"] != catalog_version_digest(catalog["version"], catalog["content_hash"]):
            raise CardVersionRefused("stage_catalog_moved")
        cards = plan["candidate_value"]["cards"]
        # A creation, and an invitation recreating a person's C or My on its stable id, is the plain upsert:
        # base_version null, so the store overwrites whatever is current (operator: "UPSERT. overwrite";
        # EMain 17:01Z). Q2(b), a fenced invitation, would pass the existing revision instead.
        members = [{"subject_hash": card["subject_hash"], "access_id": card["access_id"],
                    "base_version": (None if card["original_absent"] or card["action"] == "recreate"
                                     else card["original_revision"]),
                    "value": card["candidate"]}
                   for card in cards]
        effects = await self._effects(cards, at=_at(data["at"]))

        async def prepare() -> None:
            for effect in effects:
                await self._prepare(txn, effect)

        answer = await self._store.stage(
            txn, request_id=data["request_id"], request_digest=digest, catalog=dict(catalog),
            actor_subject=data["actor_subject"], actor_kind=data["actor_kind"], members=members,
            effects=effects, prepare=prepare, at=_at(data["at"]), **binding)
        return _links(answer)

    async def _effects(self, cards: Sequence[Mapping[str, Any]], *, at: datetime) -> list[dict[str, Any]]:
        """The save's Hub effects: ``handle_binding`` per edited credential-bearing Card, nothing else.

        An agent row's re-wrap envelope is prepared at the request's own ``at``, never this host's
        clock: a STAGE retry (the store calls ``prepare`` again on a ``staging`` marker) then prepares
        the identical envelope under the same ref, which the resident secret store answers as a replay.
        """
        produce = getattr(self._host, "_handle_binding_effects", None)
        if produce is None:
            return []
        pairs = []
        for card in cards:
            if not card["original_absent"]:
                original = await self._host._cards().load_current(card["access_id"],
                                                                  subject_hash=card["subject_hash"])
                pairs.append((original[0] if original is not None else None,
                              CardAuthority.from_mapping(card["candidate"])))
        effects = await produce(pairs)
        for effect in effects:
            if effect.get("kind") != HANDLE_BINDING:
                raise CardVersionRefused("edit_invalid")  # card_effect_adapter_unavailable: fail closed
        prepared_at = int(at.timestamp())
        return [{"kind": effect["kind"], "key": effect["key"], "access_id": effect["payload"]["access_id"],
                 "payload": {**effect["payload"],
                             "prepared_at": prepared_at if effect["payload"]["from_fingerprint"] else 0}}
                for effect in effects]

    def _operation(self, name: str) -> Any:
        operation = getattr(self._handles, name, None)
        if operation is None:
            raise CardVersionRefused("edit_invalid")  # card_effect_adapter_unavailable
        return operation

    async def _mutate(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """One handle-store write, finished even if the caller is cancelled (Infra K4).

        Inside the store's drain scope the write is tracked, so the Card locks are
        not released until it has returned; a cancellation never leaves half of it.
        """
        return await cancellation_safe_await(self._operation(name)(*args, **kwargs))

    async def _prepare(self, txn: str, effect: Mapping[str, Any]) -> None:
        """STAGE: the row is still exactly the pinned identity; an agent row's AFTER envelope is prepared now."""
        payload = effect["payload"]
        identity = await self._operation("binding_identity")(payload["access_id"])
        if not isinstance(identity, Mapping) or (identity.get("from_identity"), identity.get("from_revision"),
                                                 identity.get("from_expires_at")) != (
                payload["from_identity"], payload["from_revision"], payload["from_expires_at"]):
            raise CardVersionRefused("card_changed")
        if payload["from_fingerprint"]:
            try:
                await self._mutate("stage_rewrap", txn, payload["access_id"], payload)
            except ParticipantEffectRefused:
                raise CardVersionRefused("edit_invalid") from None

    async def _apply(self, effect: Mapping[str, Any], marker: Mapping[str, Any]) -> str:
        """PUBLISH: one effect, answered by its named result or ``effects_pending`` (contract section 4).

        Any failure of the handle store is ``effects_pending``: the effect stays unrecorded, and the txn's
        PUBLISH retry or ROLLBACK runs it again (it is idempotent by txn and key).
        """
        try:
            return await self._apply_once(effect, marker)
        except CardVersionRefused:
            raise
        except Exception:  # noqa: BLE001 - the handle store's own fault, never internal text to PB
            raise CardVersionRefused("effects_pending") from None

    async def _apply_once(self, effect: Mapping[str, Any], marker: Mapping[str, Any]) -> str:
        """Move the row only while current.json still names this txn's version of the Card."""
        payload, txn = effect["payload"], marker["txn"]
        member = next((member for member in marker["members"]
                       if member["card"]["access_id"] == effect["access_id"]), None)
        if member is None:
            raise CardVersionRefused("effects_pending")
        current = await self._store.read_current(member["card"]["subject_hash"], effect["access_id"])
        live = (isinstance(current, Mapping) and (current.get("version"), current.get("checksum"))
                == (member["version"], member["checksum"]))
        if not live:
            if payload["from_fingerprint"]:
                await self._mutate("discard_rewrap", txn, payload["access_id"], payload)
            return CREDENTIAL_ISSUE_SUPERSEDED
        if payload["from_fingerprint"]:
            outcome = await self._mutate("commit_rewrap", txn, payload["access_id"], payload)
        else:
            outcome = await self._mutate("advance_binding",
                payload["access_id"], from_identity=payload["from_identity"], from_revision=payload["from_revision"],
                from_expires_at=payload["from_expires_at"], to_revision=payload["card_revision"],
                to_expires_at=payload["expires_at"])
        if outcome in ("applied", CREDENTIAL_ISSUE_SUPERSEDED):
            return outcome
        raise CardVersionRefused("effects_pending")

    async def _release(self, effect: Mapping[str, Any], marker: Mapping[str, Any]) -> None:
        """ROLLBACK of a staged txn: an agent row's prepared envelope goes; the active row never moved."""
        payload = effect["payload"]
        if payload["from_fingerprint"]:
            await self._mutate("discard_rewrap", marker["txn"], payload["access_id"], payload)

    def _signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                result: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_digest": digest, **{name: data[name] for name in ECHO_FIELDS},
                    "result": dict(result)}
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.CARD_VERSION)
        return {"ok": result.get("kind") != "refused", "participant_answer": {**unsigned, "receipt_proof": proof}}


__all__ = ["ANSWER_SCHEMA", "CardVersionOperation", "CardVersionRefused", "CardVersionStore", "ECHO_FIELDS",
           "OPERATION", "REFUSALS", "REQUEST_FIELDS", "REQUEST_SCHEMA", "card_version_request_digest",
           "stage_authorization", "stage_digest"]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Resident original plans over the existing issuance table and Hub group protocol.

The stored plan, linked Card files and real decision are the recovery record.
There is no bearer in a plan and no second decision or custody log here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import logging
from typing import Any, Mapping, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused
from connection_hub.authority_registry import DELEGATED_CLIENT_AUTHORITY_ID

from .caller_writer_gate import candidate_shape_refusal
from .caller_writer_gate import binding_of, caller_writer_before_commit, caller_write_outcome, CallerWriteRefused
from .catalog.reservations import catalog_version_digest
from .cards.card_group import group_member, hub_group_participant_input
from .cards.card_participant import PARTICIPANT
from .cards.resident_issuance import RESIDENT_ISSUANCE_KIND
from .cards.store import subject_hash_for
from .cards.service import CardConflict
from .cards.transaction_store import CardTransactionRefused
from .cards.version_link import planned_link, pointer_link, staging_tag, write_hidden_version
from .descriptor_agent_card import (
    DescriptorAgentCardRefused, validate_descriptor_agent_additions, validate_descriptor_agent_control,
)
from .oauth.grants import integration_subject
from .issuer_gate import IssuerWriteRefused, change_digest
from .oauth_issuance import (
    ISSUANCE_PLAN_SCHEMA, IssuanceRefused, OAuthIssuanceResult, SlotOutcome,
    credential_issue_effect, decision_request_id, effect_digest, original_input_digest,
)

LOGGER = logging.getLogger(__name__)


class ResidentIssuancePending(DescriptorAgentCardRefused):
    def __init__(self, reason: str, *, control_fallback: bool = False, retryable: bool = True) -> None:
        super().__init__(reason, retryable=retryable, status=503 if retryable else 409)
        self.control_fallback = control_fallback


def _inputs(before: Any, candidate: Any, prior_control: Any, control: Any, *,
            catalog: str, lease_seconds: int, effects: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected = candidate.to_dict()
    selected["expires_at"] = 0  # the stored planning clock plus the explicit lease policy sets this
    if before is None:
        selected["created_at"] = 0
    ceiling = control.to_dict()
    if prior_control is None:
        ceiling["created_at"] = 0
    return {"kind": RESIDENT_ISSUANCE_KIND, "original": before.to_dict() if before else None,
        "candidate": selected, "original_control": prior_control.to_dict() if prior_control else None,
        "control": ceiling, "catalog": catalog, "lease_seconds": lease_seconds,
        "effects": [dict(effect) for effect in effects]}


async def retire_aborted_original(service: Any, *, transaction_id: str) -> bool:
    """Retire even an original prepared before STAGE, using an actual ABORT.

    No effect receipt is required: abort_unstaged intentionally has no effect
    release. A deadline alone never establishes this authority. Repeated
    cleanup and a later effect release use the same empty-digest terminal
    context, including when no Hub slot had been reserved at the first cut.
    """
    _, _, decisions, _, store = service._issuance_parts()
    raw, plan, row = await service._trusted_issuance(transaction_id, decisions=decisions, store=store,
                                                   snapshot=False)
    if raw.get("issuance_kind") != RESIDENT_ISSUANCE_KIND or row.state != "aborted":
        return False
    provider = getattr(service, "_resident_credentials", None)
    if not callable(getattr(provider, "retire", None)):
        raise IssuanceRefused("resident_issuance_adapter_unavailable", retryable=True)
    await store.release_issued_credential(transaction_id=transaction_id, slot="access")
    result = OAuthIssuanceResult(transaction_id=transaction_id, intent_digest=row.intent.digest, state="aborted",
        access_id=plan.access_id, card_revision=plan.base_revision, expires_at=plan.expires_at,
        delivery_deadline=plan.delivery_deadline, receipt_digest="",
        per_slot={"access": SlotOutcome("released", plan.effect_digests["access"], "")})
    await provider.retire(plan=plan, result=result)
    await store.complete_resident_original_cleanup(transaction_id=transaction_id)
    return True


async def _planning_clock(service: Any, store: Any, *, request: str, ttl: int) -> int:
    now = await store.issuance_clock()
    attempt = await store.read_plan_attempt(request)
    if attempt is None:
        now = await store.pin_plan_attempt(decision_request_id=request, planned_at=now, expires_at=now + ttl)
    elif attempt["expired"]:
        if service.oauth_candidate_cleanup_enabled() and attempt.get("candidate"):
            await service._discard_planned_candidate(attempt["candidate"], request)
        now = await store.renew_expired_plan_attempt(decision_request_id=request,
            expected_planned_at=attempt["planned_at"], planned_at=now, expires_at=now + ttl)
    else:
        now = attempt["planned_at"]
    if now is None:
        raise IssuanceRefused("resident_issuance_planning_busy", retryable=True)
    return now


async def _pointer(store: Any, original: Any, candidate: Any) -> Any:
    current = await store.read_current_authority(subject_hash=subject_hash_for(candidate.grantor_subject),
                                                access_id=candidate.access_id)
    if original is None:
        if current is not None:
            raise IssuanceRefused("card_intent_base_moved", retryable=True)
        return None
    if (current is None or current[1].card_revision != original.card_revision
            or current[1].content_hash() != original.content_hash()):
        raise IssuanceRefused("card_intent_base_moved", retryable=True)
    return pointer_link(current[0])


async def _store_plan(service: Any, store: Any, *, before: Any, candidate: Any, prior_control: Any,
                      control: Any, original_record: Any, request: str, logical: str, ordinal: int,
                      input_digest: str, catalog: str, lease_seconds: int, ttl: int,
                      extra_effects: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    now = await _planning_clock(service, store, request=request, ttl=ttl)
    candidate = replace(candidate, created_at=now if before is None else candidate.created_at,
                         expires_at=now + lease_seconds)
    control = replace(control, created_at=now) if prior_control is None else control
    validate_descriptor_agent_control(candidate, control, tenant=service._tenant, project=service._project,
                                      grantor_subject=candidate.grantor_subject)
    action = "create" if before is None else "renew"
    refusal = candidate_shape_refusal(action, before.to_dict() if before else None, candidate.to_dict())
    if refusal is not None:
        raise IssuanceRefused(refusal)
    if before is not None:
        validate_descriptor_agent_additions(before, candidate, control)
    card_store = getattr(service._cards(), "card_store", None)
    if card_store is None:
        raise IssuanceRefused("card_transactions_unavailable", retryable=True)
    effects = [credential_issue_effect(access_id=candidate.access_id, slot="access",
        expires_at=candidate.expires_at, card_revision=candidate.card_revision)]
    if original_record is not None and original_record.access_token:
        if not original_record.session_id:
            raise IssuanceRefused("resident_issuance_previous_session_missing")
        effects.append({"kind": "grant_unbind", "key": "previous-resident", "payload": {
            "access_id": candidate.access_id, "session_id": original_record.session_id,
            "token_sha256": hashlib.sha256(original_record.access_token.encode("utf-8")).hexdigest()}})
    effects.extend(dict(effect) for effect in extra_effects)
    changed_control = prior_control != control
    pairs = [(before, candidate)]
    if changed_control:
        pairs.append((prior_control, control))
    pairs.sort(key=lambda pair: (subject_hash_for(pair[1].grantor_subject), pair[1].access_id))
    if pairs[0][1].access_id != candidate.access_id:
        raise IssuanceRefused("resident_issuance_group_lead_invalid")
    reads = [] if changed_control else [{"subject_hash": subject_hash_for(control.grantor_subject),
        "access_id": control.access_id, "revision": control.card_revision}]

    def member_action(old: Any, new: Any) -> str:
        # Keep the same action at planning, caller policy and Hub STAGE. Only
        # the resident rotates its lease; the existing Control is an update.
        return "create" if old is None else "renew" if new.access_id == candidate.access_id else "update"

    projection = hub_group_participant_input(
        members=[group_member(original=old, candidate=new, action=member_action(old, new))
                 for old, new in pairs], actor_subject=candidate.grantor_subject, actor_kind="caller",
        effects=effects, reads=reads, catalog_version_digest=catalog)
    at = datetime.fromtimestamp(now, tz=timezone.utc)
    tag = staging_tag("oauth-issuance-candidate", request)
    members = []
    for old, new in pairs:
        members.append({"subject_hash": subject_hash_for(new.grantor_subject), "access_id": new.access_id,
            "original": await _pointer(card_store, old, new),
            "candidate": planned_link(authority=new, at=at, tag=tag),
            "action": member_action(old, new)})
    recorded = {"members": [{"link": member["candidate"], "subject_hash": member["subject_hash"],
        "access_id": member["access_id"]} for member in members]}
    attempt = await store.read_plan_attempt(request)
    if service.oauth_candidate_cleanup_enabled() and attempt and attempt.get("candidate") \
            and attempt["candidate"] != recorded:
        await service._discard_planned_candidate(attempt["candidate"], request)
    if await store.record_attempt_candidate(decision_request_id=request, planned_at=now, candidate=recorded) is False:
        raise IssuanceRefused("resident_issuance_planning_busy", retryable=True)
    for (_, new), member in zip(pairs, members):
        written = await write_hidden_version(card_store, subject_hash=member["subject_hash"], authority=new, at=at, tag=tag)
        if written != member["candidate"]:
            raise IssuanceRefused("issuance_plan_card_unavailable")
    plan = {"schema": ISSUANCE_PLAN_SCHEMA, "issuance_kind": RESIDENT_ISSUANCE_KIND,
        "resident_request": logical, "resident_attempt": ordinal,
        "draft": {"replay_scope": f"{PARTICIPANT}:{subject_hash_for(candidate.grantor_subject)}:resident-issuance",
            "request_id": request, "expires_at": now + ttl,
            "payload": {"participant_inputs": {PARTICIPANT: projection}}},
        "intent": {"shape": "group", **members[0], "members": members, "card_kind": candidate.card_kind,
            "effects": effects, "actor_subject": candidate.grantor_subject, "actor_kind": "caller",
            "reads": reads, "catalog": catalog},
        "decision_request_id": request, "original_input_digest": input_digest,
        "tenant": store.tenant, "project": store.project, "access_id": candidate.access_id,
        "grantor_subject": candidate.grantor_subject, "client_id": candidate.client_id,
        "credential_issuer": DELEGATED_CLIENT_AUTHORITY_ID,
        "credential_subject": integration_subject(candidate.grantor_subject, client_id=candidate.client_id),
        "base_revision": before.card_revision if before else 0, "candidate_revision": candidate.card_revision,
        "expires_at": candidate.expires_at, "card_content_hash": candidate.content_hash(),
        "delivery_deadline": candidate.expires_at, "reserved_until": min(now + ttl, candidate.expires_at),
        "slots": ["access"], "planned_at": now,
        "effect_digests": {effect["key"]: effect_digest(effect) for effect in effects}}
    stored = await store.put_issuance_plan(decision_request_id=request, original_input_digest=input_digest,
                                          plan=plan, reserved_until=plan["reserved_until"])
    await store.delete_plan_attempt(request, planned_at=now)
    return stored["plan"]


async def begin_descriptor_resident_issuance(service: Any, user: Mapping[str, Any], *, original: Any,
                                             resident: Any, original_control: Any, control: Any,
                                             active_catalog: Any, lease_seconds: int,
                                             effects: Sequence[Mapping[str, Any]] = ()) -> Any:
    """Return the same original while uncertain; only a real ABORT permits another attempt."""
    from .automation_access import card_authority_from_record, _subject_from_user
    from .descriptor_agent_lifecycle import _prior_binding

    _, intents, decisions, ttl, store = service._issuance_parts()
    actor = _subject_from_user(user)
    before = card_authority_from_record(original) if original is not None else None
    candidate, ceiling = card_authority_from_record(resident), card_authority_from_record(control)
    prior_control = card_authority_from_record(original_control) if original_control is not None else None
    if actor != candidate.grantor_subject or type(lease_seconds) is not int or not 1 <= lease_seconds <= 400 * 86400:
        raise IssuanceRefused("resident_issuance_request_invalid")
    provider, handles = getattr(service, "_resident_credentials", None), getattr(service, "_card_credential_handles", None)
    if (any(not callable(getattr(provider, name, None)) for name in ("prepare", "read", "activate", "retire", "retire_previous"))
            or not callable(getattr(handles, "write", None))):
        raise IssuanceRefused("resident_issuance_adapter_unavailable", retryable=True)
    validate_descriptor_agent_control(candidate, ceiling, tenant=service._tenant, project=service._project,
                                      grantor_subject=actor)
    if before is not None:
        if prior_control is None:
            raise IssuanceRefused("agent_capability_control_not_active")
        validate_descriptor_agent_control(_prior_binding(before, prior_control), prior_control,
            tenant=service._tenant, project=service._project, grantor_subject=actor)
    if prior_control is None and service._issuer_managed(control):
        raise DescriptorAgentCardRefused("agent_descriptor_control_creation_authority_unsupported")
    catalog = catalog_version_digest(active_catalog.version, active_catalog.content_hash)
    if ceiling.catalog_version != active_catalog.version:
        raise IssuanceRefused("agent_capability_control_catalog_conflict", retryable=True)
    digest = original_input_digest(_inputs(before, candidate, prior_control, ceiling,
                                          catalog=catalog, lease_seconds=lease_seconds, effects=effects))
    logical = decision_request_id(scope=f"{store.tenant}:{store.project}:{RESIDENT_ISSUANCE_KIND}",
        grantor_subject=actor, client_id=candidate.client_id, original_request_id=digest)
    async with service._oauth_request_section(logical):
        latest = await store.latest_resident_issuance(logical)
        ordinal = 0
        if latest is not None:
            raw, transaction_id = latest["plan"], latest["transaction_id"]
            row = await decisions.read(transaction_id) if transaction_id else None
            if transaction_id and row is None:
                raise IssuanceRefused("issuance_plan_unknown")
            if row is not None and not row.terminal:
                try:
                    row = await decisions.abort_expired(transaction_id)
                except DecisionRefused as exc:
                    if str(exc) != "not_expired":
                        raise
            if row is None or row.state != "aborted":
                if row is not None and row.state == "committed":
                    return await service._issuance_plan(raw, transaction_id, row.intent.digest)
                return await service._begin_planned_issuance(raw, intents=intents, decisions=decisions, store=store)
            await service._card_coordinator[0].finish(transaction_id)
            await retire_aborted_original(service, transaction_id=transaction_id)
            ordinal = raw["resident_attempt"] + 1
        request = decision_request_id(scope=RESIDENT_ISSUANCE_KIND, grantor_subject=actor,
            client_id=candidate.client_id, original_request_id=f"{logical}:{ordinal}")
        from .descriptor_agent_selection import validate_new_standard_authority

        await validate_new_standard_authority(service, user, original=original, resident=resident, active=active_catalog)
        raw = await _store_plan(service, store, before=before, candidate=candidate, prior_control=prior_control,
            control=ceiling, original_record=original, request=request, logical=logical, ordinal=ordinal,
            input_digest=digest, catalog=catalog, lease_seconds=lease_seconds, ttl=ttl, extra_effects=effects)
        return await service._begin_planned_issuance(raw, intents=intents, decisions=decisions, store=store)


async def decide_descriptor_resident_issuance(service: Any, raw: Mapping[str, Any], plan: Any, *,
                                              coordinator: Any, renew: Any) -> tuple[bool, str]:
    """Prepare and revalidate the frozen resident/Control group before its one decision.

    Stored intent originals supply the policy question even on replay of a
    partially prepared transaction. Public Card reads would be fenced then;
    the participant still checks the original vectors and held dependencies.
    """
    from .automation_access import record_from_card

    _, intents, decisions, _, _ = service._issuance_parts()
    intent = await intents.load(plan.transaction_id)
    resident_member = next(member for member in intent.members if member.candidate.access_id == plan.access_id)
    candidate, before = resident_member.candidate, resident_member.original
    binding = candidate.control_card
    control_member = next((member for member in intent.members
                           if binding is not None and member.candidate.access_id == binding.control_id), None)
    if control_member is None:
        found = await service._load_record_any_state(binding.control_id, grantor_subject=plan.grantor_subject)
        if found is None or found[1] != "active":
            raise IssuanceRefused("agent_capability_control_not_active")
        from .automation_access import card_authority_from_record

        ceiling = card_authority_from_record(found[0])
    else:
        ceiling = control_member.candidate
    caller_request = issuer_request = None
    reason = ""
    committed_here = False
    known = (DecisionRefused, CardTransactionRefused, CallerWriteRefused, IssuerWriteRefused,
             CardConflict, IssuanceRefused, DescriptorAgentCardRefused)
    try:
        validate_descriptor_agent_control(candidate, ceiling, tenant=service._tenant, project=service._project,
                                          grantor_subject=plan.grantor_subject)
        caller_gate = None
        registry = getattr(service, "_caller_writers", None)
        governed_binding = binding_of(before) if before is not None else binding_of(candidate)
        if registry is not None and registry.is_bound(governed_binding[0]):
            caller_gate, caller_request = await caller_writer_before_commit(registry, before,
                actor_subject=plan.grantor_subject, action="create" if before is None else "renew",
                candidate=candidate.to_dict(), request_id=plan.decision_request_id, binding=governed_binding)
        issuer_gate = None
        if control_member is not None:
            if control_member.original is None:
                if service._issuer_managed(record_from_card(ceiling)):
                    raise IssuanceRefused("agent_descriptor_control_creation_authority_unsupported")
            else:
                issuer_gate, issuer_request = await service._issuer_before_commit(
                    record_from_card(control_member.original), {"user_id": plan.grantor_subject}, action="update",
                    candidate=ceiling.to_dict(), request_id=plan.decision_request_id, context_ref="", decision=None)
        if not await renew():
            return False, ""
        await coordinator.prepare_existing(plan.transaction_id)
        active = await service._active_catalog()
        if catalog_version_digest(active.version, active.content_hash) != raw["intent"]["catalog"]:
            raise IssuanceRefused("agent_capability_catalog_revision_conflict", retryable=True)
        for gate, request, authority in ((caller_gate, caller_request, candidate),
                                          (issuer_gate, issuer_request, ceiling)):
            if request is not None and request.change_digest != change_digest(authority.to_dict()):
                raise IssuerWriteRefused("issuer_candidate_changed")
            if gate is not None:
                await gate()
            if request is not None and request.change_digest != change_digest(authority.to_dict()):
                raise IssuerWriteRefused("issuer_candidate_changed")
        if not await renew():
            return False, ""
        projection = raw["draft"]["payload"]["participant_inputs"][PARTICIPANT]
        witness = (caller_request.change_digest if caller_request is not None else
                   issuer_request.change_digest if issuer_request is not None else projection["candidate_digest"])
        await coordinator.decide(plan.transaction_id, "committed", witness_digest=witness)
        committed_here = True
    except known as exc:
        reason = str(getattr(exc, "reason", "") or exc)[:128]
        if await renew():
            try:
                await coordinator.decide(plan.transaction_id, "aborted")
            except DecisionRefused:
                pass  # an actual recorded decision wins
    finally:
        actual = await decisions.read(plan.transaction_id)
        if actual is not None and actual.terminal:
            committed = actual.state == "committed"
            state = "committed" if committed else "refused"
            await caller_write_outcome(getattr(service, "_caller_writers", None), caller_request, state=state,
                card_revision=candidate.card_revision if committed else plan.base_revision)
            await service._issuer_outcome(issuer_request, state=state,
                card_revision=ceiling.card_revision if committed else
                control_member.original.card_revision if control_member is not None and control_member.original else 0)
    return committed_here, reason


async def _abort_before_completion(service: Any, plan: Any) -> bool:
    """True only after an actual ABORT finished, so a Control-only fallback is safe."""
    coordinator, _, decisions, _, _ = service._issuance_parts()
    try:
        row = await decisions.read(plan.transaction_id)
        if row is None:
            return False
        if not row.terminal:
            try:
                await coordinator.decide(plan.transaction_id, "aborted")
            except DecisionRefused:
                pass
            row = await decisions.read(plan.transaction_id)
        if row is None or row.state != "aborted":
            return False  # a concurrent COMMIT wins; never undo it or replace its original
        row = await coordinator.finish(plan.transaction_id)
        try:
            await retire_aborted_original(service, transaction_id=plan.transaction_id)
        except Exception:
            # No usable original exists for an actual ABORT. The bounded
            # cleanup pass retries its SDK tombstone if that authority is down.
            LOGGER.warning("[descriptor-resident-issuance] inactive retirement pending transaction=%s", plan.transaction_id)
        return row.state == "aborted" and PARTICIPANT in row.finished
    except Exception:
        LOGGER.warning("[descriptor-resident-issuance] abort recovery pending transaction=%s", plan.transaction_id)
        return False


async def _control_fallback_without_commit(service: Any, user: Mapping[str, Any], *, original: Any,
        resident: Any, original_control: Any, control: Any, active_catalog: Any, lease_seconds: int,
        effects: Sequence[Mapping[str, Any]]) -> bool:
    """Check the plan/decision, independently of the failure's reason.

    Plan persistence precedes BEGIN. Under its same request section, absence
    therefore proves this issuance never began. A stored but unbound plan
    must replay BEGIN/binding before we can authenticate and finish ABORT.
    Lost responses and actual COMMITs never authorize this fallback.
    """
    from .automation_access import card_authority_from_record, _subject_from_user

    store = getattr(service, "_oauth_issuance_store", None)
    if store is None:
        return True  # composition preflight failed before any planner write
    try:
        _, intents, decisions, _, _ = service._issuance_parts()
        actor = _subject_from_user(user)
        before = card_authority_from_record(original) if original is not None else None
        prior = card_authority_from_record(original_control) if original_control is not None else None
        candidate, ceiling = card_authority_from_record(resident), card_authority_from_record(control)
        digest = original_input_digest(_inputs(before, candidate, prior, ceiling,
            catalog=catalog_version_digest(active_catalog.version, active_catalog.content_hash),
            lease_seconds=lease_seconds, effects=effects))
        logical = decision_request_id(scope=f"{store.tenant}:{store.project}:{RESIDENT_ISSUANCE_KIND}",
            grantor_subject=actor, client_id=candidate.client_id, original_request_id=digest)
        async with service._oauth_request_section(logical):
            latest = await store.latest_resident_issuance(logical)
            if latest is None:
                return True
            if latest["transaction_id"]:
                row = await decisions.read(latest["transaction_id"])
                if row is None or row.state == "committed":
                    return False
                plan = await service._issuance_plan(latest["plan"], row.transaction_id, row.intent.digest, snapshot=False)
            else:
                plan = await service._begin_planned_issuance(latest["plan"], intents=intents, decisions=decisions, store=store)
            return await _abort_before_completion(service, plan)
    except Exception:
        return False  # an unknown decision or unavailable authority remains fenced


async def issue_descriptor_resident(service: Any, user: Mapping[str, Any], *, original: Any, resident: Any,
                                    original_control: Any, control: Any, active_catalog: Any,
                                    lease_seconds: int, effects: Sequence[Mapping[str, Any]] = ()) -> Any:
    """Prepare inactive only after BEGIN; return custody only after the one FINISH."""
    try:
        plan = await begin_descriptor_resident_issuance(service, user, original=original, resident=resident,
            original_control=original_control, control=control, active_catalog=active_catalog,
            lease_seconds=lease_seconds, effects=effects)
    except (IssuanceRefused, DescriptorAgentCardRefused, CallerWriteRefused, IssuerWriteRefused) as exc:
        reason = "resident_issuance_adapter_unavailable" if exc.reason == "card_transactions_unavailable" else exc.reason
        fallback = await _control_fallback_without_commit(service, user, original=original, resident=resident,
            original_control=original_control, control=control, active_catalog=active_catalog,
            lease_seconds=lease_seconds, effects=effects)
        raise ResidentIssuancePending(reason, retryable=getattr(exc, "retryable", False),
                                      control_fallback=fallback) from None
    except Exception:
        fallback = await _control_fallback_without_commit(service, user, original=original, resident=resident,
            original_control=original_control, control=control, active_catalog=active_catalog,
            lease_seconds=lease_seconds, effects=effects)
        raise ResidentIssuancePending("resident_issuance_planning_unavailable", control_fallback=fallback) from None
    provider = service._resident_credentials
    _, _, decisions, _, _ = service._issuance_parts()
    row = await decisions.read(plan.transaction_id)
    if row is None:
        raise ResidentIssuancePending("issuance_plan_unknown", retryable=False)
    if not row.terminal:
        try:
            try:
                prepared = await provider.read(plan=plan)
            except Exception as exc:
                if getattr(exc, "reason", None) != "issuance_reservation_missing":
                    raise
                prepared = await provider.prepare(plan=plan)
            await service.reserve_oauth_issuance(plan=plan, slot="access",
                token_sha256=prepared.receipt.bearer_sha256, record=prepared.record, ttl_seconds=prepared.ttl_seconds)
        except BaseException as exc:
            fallback = await _abort_before_completion(service, plan)
            if not isinstance(exc, Exception):
                raise
            # Exception text and provider records may contain credentials;
            # expose only this bounded operation code, never those values.
            raise ResidentIssuancePending("resident_issuance_prepare_unavailable", control_fallback=fallback) from None
    try:
        result = await service.complete_oauth_issuance(transaction_id=plan.transaction_id)
    except (IssuanceRefused, CallerWriteRefused, IssuerWriteRefused, DescriptorAgentCardRefused) as exc:
        fallback = await _abort_before_completion(service, plan)
        raise ResidentIssuancePending(getattr(exc, "reason", "resident_issuance_pending"),
            retryable=getattr(exc, "retryable", False), control_fallback=fallback) from None
    if result.state == "aborted":
        fallback = await _abort_before_completion(service, plan)
        raise ResidentIssuancePending(result.reason or "resident_issuance_aborted", control_fallback=fallback)
    if result.state != "committed":
        raise ResidentIssuancePending("resident_issuance_pending")
    if result.per_slot["access"].outcome != "applied":
        raise ResidentIssuancePending("resident_issuance_superseded")
    loaded = await service._load_record_any_state(plan.access_id, grantor_subject=plan.grantor_subject)
    if loaded is None or loaded[1] != "active" or not loaded[0].access_token:
        raise ResidentIssuancePending("resident_issuance_custody_unavailable")
    return loaded[0]


async def recover_resident_originals(service: Any, *, after: str = "", limit: int = 50) -> dict[str, Any]:
    """One bounded page: authenticate actual ABORT before retiring an inactive original."""
    _, _, decisions, _, store = service._issuance_parts()
    page = await store.resident_issuance_cleanup_page(after=after, limit=limit)
    retired = failed = 0
    for entry in page["items"]:
        transaction_id = entry["transaction_id"]
        try:
            row = await decisions.read(transaction_id)
            if row is not None and row.state == "aborted":
                retired += int(await retire_aborted_original(service, transaction_id=transaction_id))
            elif row is not None and row.state == "committed" and PARTICIPANT in row.finished:
                await store.complete_resident_original_cleanup(transaction_id=transaction_id)
        except Exception:
            failed += 1
            LOGGER.warning("[descriptor-resident-issuance] terminal cleanup pending transaction=%s", transaction_id)
    return {"retired": retired, "failed": failed, "next_after": page["next_after"]}


__all__ = ["ResidentIssuancePending", "begin_descriptor_resident_issuance", "decide_descriptor_resident_issuance",
           "issue_descriptor_resident", "recover_resident_originals", "retire_aborted_original"]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Descriptor sync's Control/resident group over the ordinary Hub participant."""

from __future__ import annotations

from dataclasses import replace
import logging
import secrets
import time
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused, IntentDraft

from .caller_writer_gate import CallerWrite, caller_write_outcome, candidate_shape_refusal
from .catalog.reservations import catalog_version_digest
from .cards.card_group import group_member, hub_group_participant_input
from .cards.card_participant import PARTICIPANT, CardGroupIntent, CardGroupMemberIntent
from .cards.store import subject_hash_for
from .cards.transaction_store import CardTransactionRefused
from .descriptor_agent_card import (
    DescriptorAgentCardRefused, validate_descriptor_agent_additions, validate_descriptor_agent_control,
)
from .issuer_gate import IssuerWriteRefused, change_digest

LOGGER = logging.getLogger(__name__)


def _prior_binding(resident: Any, control: Any) -> Any:
    """Only sync may advance a stale snapshot of the SAME canonical Control.

    Owner Save remains strict. The exact current Control is reserved or a
    group member here; a future revision never authorizes an advance.
    """
    binding = resident.control_card
    if binding is None or binding.control_revision > control.card_revision:
        raise DescriptorAgentCardRefused("agent_capability_control_revision_conflict", retryable=True)
    return replace(resident, control_card=replace(binding, control_revision=control.card_revision))


async def persist_descriptor_lifecycle(service: Any, user: Mapping[str, Any], *, original: Any,
                                       resident: Any, original_control: Any, control: Any,
                                       active_catalog: Any) -> Any:
    """Commit an existing bearer's descriptor sync, including a changed Control.

    Return the current resident, or the named pending-issuance refusal after
    an independent Control group committed. A Control change is independent
    of bearer availability and may commit while issuance remains pending.
    No path here invokes the ordinary active minter.
    """
    from .automation_access import card_authority_from_record, _subject_from_user

    actor = _subject_from_user(user)
    candidate, ceiling = card_authority_from_record(resident), card_authority_from_record(control)
    before = card_authority_from_record(original) if original is not None else None
    prior_control = card_authority_from_record(original_control) if original_control is not None else None
    if actor != candidate.grantor_subject:
        raise DescriptorAgentCardRefused("agent_capability_card_not_owned", status=403)
    validate_descriptor_agent_control(candidate, ceiling, tenant=service._tenant, project=service._project,
                                      grantor_subject=actor)
    if before is not None:
        if prior_control is None:
            raise DescriptorAgentCardRefused("agent_capability_control_not_active")
        validate_descriptor_agent_control(_prior_binding(before, prior_control), prior_control,
            tenant=service._tenant, project=service._project, grantor_subject=actor)
        if candidate_shape_refusal("update", before.to_dict(), candidate.to_dict()) is not None:
            raise DescriptorAgentCardRefused("agent_capability_edit_identity_conflict")
        validate_descriptor_agent_additions(before, candidate, ceiling)
    # Project authority ports never govern this descriptor path. An explicit
    # dedicated descriptor issuer registration is a separate policy, and its
    # update/revoke contract cannot authorize creation by pretending absence
    # was an existing Card. Keep that exceptional case fail closed.
    if prior_control is None and service._issuer_managed(control):
        raise DescriptorAgentCardRefused("agent_descriptor_control_creation_authority_unsupported")
    changed_control = prior_control != ceiling
    credential_needed = original is None or not original.access_token or original.expires_at <= int(time.time())
    issuance_error = None
    if credential_needed:
        from .agent_capability_sync import AGENT_CAPABILITY_CARD_LEASE_SECONDS
        from .descriptor_resident_issuance import issue_descriptor_resident, ResidentIssuancePending

        try:
            return await issue_descriptor_resident(service, user, original=original, resident=resident,
                original_control=original_control, control=control, active_catalog=active_catalog,
                lease_seconds=AGENT_CAPABILITY_CARD_LEASE_SECONDS)
        except ResidentIssuancePending as exc:
            if original is None or not changed_control or not exc.control_fallback:
                raise
            issuance_error = exc
    if (resident.access_token, resident.refresh_token, resident.session_id) != (
            original.access_token, original.refresh_token, original.session_id):
        raise DescriptorAgentCardRefused("agent_capability_credential_change_not_enlisted", status=409)

    catalog_digest = catalog_version_digest(active_catalog.version, active_catalog.content_hash)
    if ceiling.catalog_version != active_catalog.version:
        raise DescriptorAgentCardRefused("agent_capability_control_catalog_conflict", retryable=True)
    if not changed_control:
        # The common same-Control case is the already-qualified owner-edit
        # decision, after advancing only this lifecycle's old binding snapshot.
        # Passing the actual original preserves its revision/content fence.
        if before.control_card.control_revision == ceiling.card_revision:
            await service._persist_descriptor_agent_edit(original, resident, control=control,
                active_catalog=active_catalog, actor_subject=actor)
            return resident

    request_id = secrets.token_urlsafe(18)
    caller = CallerWrite("update", actor, request_id=request_id)
    caller_request = issuer_request = None
    members = [(before, candidate)]
    if changed_control:
        members.append((prior_control, ceiling))
    members.sort(key=lambda pair: (subject_hash_for(pair[1].grantor_subject), pair[1].access_id))
    reads = () if changed_control else ({"subject_hash": subject_hash_for(actor),
        "access_id": ceiling.access_id, "revision": ceiling.card_revision},)
    effects = await service._handle_binding_effects(members)
    bound = getattr(service, "_card_coordinator", None)
    if bound is None:
        raise DescriptorAgentCardRefused("agent_capability_transaction_unavailable", retryable=True, status=503)
    coordinator, intents, decisions, ttl = bound
    participant_input = hub_group_participant_input(
        members=[group_member(original=old, candidate=new, action="create" if old is None else "update")
                 for old, new in members],
        actor_subject=actor, actor_kind="caller", effects=effects, reads=reads,
        catalog_version_digest=catalog_digest)
    draft = IntentDraft(replay_scope=f"{PARTICIPANT}:{subject_hash_for(actor)}:{actor}:descriptor-sync",
        request_id=request_id, expires_at=int(time.time()) + ttl, participants=(PARTICIPANT,),
        payload={"participant_inputs": {PARTICIPANT: participant_input}})
    row = None
    committed = False
    outcome_known = True
    try:
        caller_gate, caller_request = await service._enlisted_gate(
            candidate, expected_revision=before.card_revision, caller_write=caller)
        issuer_gate = None
        if changed_control and original_control is not None:
            issuer_gate, issuer_request = await service._issuer_before_commit(
                original_control, user, action="update", candidate=ceiling.to_dict(), request_id=request_id,
                context_ref="", decision=None)
        row = await decisions.begin(draft)
        await intents.record(CardGroupIntent(transaction_id=row.transaction_id, intent_digest=row.intent.digest,
            members=tuple(CardGroupMemberIntent(subject_hash=subject_hash_for(actor), original=old, candidate=new,
                action="create" if old is None else "update") for old, new in members),
            effects=tuple(effects), actor_subject=actor, actor_kind="caller", reads=reads, catalog=catalog_digest))
        await coordinator.prepare_existing(row.transaction_id)
        fresh = await service._active_catalog()
        if catalog_version_digest(fresh.version, fresh.content_hash) != catalog_digest:
            raise DescriptorAgentCardRefused("agent_capability_catalog_revision_conflict", retryable=True)
        for gate, request, authority in ((caller_gate, caller_request, candidate),
                                          (issuer_gate, issuer_request, ceiling)):
            if request is not None and request.change_digest != change_digest(authority.to_dict()):
                raise IssuerWriteRefused("issuer_candidate_changed")
            if gate is not None:
                await gate()
            if request is not None and request.change_digest != change_digest(authority.to_dict()):
                raise IssuerWriteRefused("issuer_candidate_changed")
        witness = (caller_request.change_digest if caller_request is not None else
                   issuer_request.change_digest if issuer_request is not None else participant_input["candidate_digest"])
        outcome_known = False
        await coordinator.decide(row.transaction_id, "committed", witness_digest=witness)
        committed = True
        outcome_known = True
        await coordinator.finish(row.transaction_id)
    except BaseException as exc:
        if row is not None and not committed:
            # Re-read after an uncertain decision write. Never report a real
            # COMMIT as refused or attempt to undo it.
            actual = await decisions.read(row.transaction_id)
            committed = actual is not None and actual.state == "committed"
            outcome_known = actual is not None and actual.terminal
            if not committed:
                try:
                    await coordinator.decide(row.transaction_id, "aborted")
                except DecisionRefused:
                    pass
                actual = await decisions.read(row.transaction_id)
                committed = actual is not None and actual.state == "committed"
                outcome_known = actual is not None and actual.terminal
                try:
                    await coordinator.finish(row.transaction_id)
                except Exception:
                    # Recovery retries FINISH. Keep the original refusal as
                    # this operation's cause and its structured error code.
                    LOGGER.warning("[descriptor-agent-lifecycle] abort FINISH pending transaction=%s", row.transaction_id)
        if isinstance(exc, (DecisionRefused, CardTransactionRefused)):
            raise DescriptorAgentCardRefused(str(exc), retryable=True) from None
        raise
    finally:
        if outcome_known:
            state = "committed" if committed else "refused"
            await caller_write_outcome(getattr(service, "_caller_writers", None), caller_request,
                state=state, card_revision=candidate.card_revision if committed else before.card_revision)
            await service._issuer_outcome(issuer_request, state=state,
                card_revision=ceiling.card_revision if committed else prior_control.card_revision if prior_control else 0)
    return issuance_error if credential_needed else resident


__all__ = ["persist_descriptor_lifecycle"]

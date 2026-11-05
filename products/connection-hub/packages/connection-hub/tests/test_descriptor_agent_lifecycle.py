"""Existing resident sync and selection over the real filesystem participant.

Decision/catalog/custody ports are test ports; SQL and SDK serving qualification
belong to the paired issuance tests and the final off-host gate.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time

import pytest

from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY, AGENT_DESCRIPTOR_ISSUER_KIND,
)
from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_SELECTION_PROPERTY
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from test_delegated_access_renewal import _Policy
from test_descriptor_agent_card_transaction import _current
from test_descriptor_agent_consent import (
    OWNER, POST, READ, _consent, _consent_world, _policy_with_named_operations, _sync_arguments,
)


def _refresh_arguments(*, allow_post=True):
    arguments = _sync_arguments(allow_post=allow_post)
    arguments.update(descriptor_revision="descriptor-r2", descriptor_payload={"revision": "descriptor-r2"})
    return arguments


async def _select(service, resident, *operations):
    return await service.update_agent_capability_selection({"user_id": OWNER}, access_id=resident.access_id,
        selected_capabilities=_policy_with_named_operations(*operations), expected_card_revision=resident.card_revision)


@pytest.mark.asyncio
async def test_explicit_selection_uses_local_control_and_catalog_reservations(tmp_path, monkeypatch):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)

    async def mint(*args, **kwargs):
        raise AssertionError("a live selection must not mint")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    assert (await _select(service, resident, READ, POST))["ok"]
    saved = await _current(store, resident)
    assert saved.properties[AGENT_CAPABILITY_SELECTION_PROPERTY] == _policy_with_named_operations(READ, POST)
    row = next(iter(decisions.rows.values()))
    reads = row.intent.payload["participant_inputs"][PARTICIPANT]["dependency_revisions"]
    assert reads[f"card:{subject_hash_for(OWNER)}:{control.access_id}"] == control.card_revision
    assert any(key.startswith("catalog-active:") for key in reads)
    assert await _current(store, control) == control and service._persistence.direct == []


@pytest.mark.asyncio
async def test_new_selection_above_ceiling_returns_nonretryable_code_before_a_decision(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path, allow_post=False)
    answer = await _select(service, resident, READ, POST)
    assert answer["error"] == "agent_capability_selection_outside_control" and answer["retryable"] is False
    assert decisions.rows == {} and await _current(store, resident) == resident


@pytest.mark.asyncio
async def test_control_withdrawal_and_resident_binding_commit_as_one_group(tmp_path):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)
    assert (await _consent(service, resident))["ok"]
    saved = await _current(store, resident)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments(allow_post=False))
    assert answer["ok"], answer
    updated_control, updated_resident = await _current(store, control), await _current(store, resident)
    assert updated_control.card_revision == control.card_revision + 1
    assert updated_resident.card_revision == saved.card_revision + 1
    assert updated_resident.control_card.control_revision == updated_control.card_revision
    assert f"slack/{POST}" not in answer["projection"]["capabilities"]["named_service_operations"]
    rows = list(decisions.rows.values())
    assert len(rows) == 2 and all(row.state == "committed" for row in rows)
    group = rows[-1].intent.payload["participant_inputs"][PARTICIPANT]
    assert group["binding_kind"] == "connection-hub.card-group"
    assert service._persistence.direct == []


class _Issuer:
    issuer_kind, adapter_id = AGENT_DESCRIPTOR_ISSUER_KIND, "synthetic-descriptor-issuer"

    def __init__(self, *, refuse_revalidation=False):
        self.calls, self.refuse_revalidation = [], refuse_revalidation

    async def prepare_context(self, request, **kwargs):
        self.calls.append("prepare")
        return "synthetic-descriptor-context"

    async def decide(self, request):
        self.calls.append("decide")
        allowed = not (self.refuse_revalidation and self.calls.count("decide") > 1)
        return allowed, "" if allowed else "synthetic_descriptor_refused", "v1", datetime.now(timezone.utc) + timedelta(seconds=45)

    async def finalize_context(self, request, *, outcome):
        self.calls.append(outcome["state"])
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True])
async def test_group_keeps_configured_control_issuer_and_registered_resident_gates(tmp_path, refuse):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)
    issuer = _Issuer(refuse_revalidation=refuse)
    registry = IssuerRegistry(owner_managed_kinds=(AGENT_DESCRIPTOR_ISSUER_KIND,))
    registry.register(issuer)
    service.bind_issuer_registry(registry, actor_subject=OWNER)
    policy = _Policy(True)
    callers = CallerWriterRegistry()
    callers.register(AGENT_DESCRIPTOR_ISSUER_KIND, policy)
    service.bind_caller_writers(callers)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments())
    assert answer["ok"] is not refuse, answer
    assert issuer.calls.count("decide") == 2
    assert ("revalidate", "update") in policy.calls
    if refuse:
        assert answer["reason"] == "synthetic_descriptor_refused" and answer["retryable"] is False
        assert await _current(store, control) == control and await _current(store, resident) == resident
        assert decisions.decisions == ["aborted"] and "refused" in issuer.calls
    else:
        assert decisions.decisions == ["committed"] and "committed" in issuer.calls
    assert service._persistence.direct == [] and await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_registered_but_unavailable_issuer_refuses_without_an_owner_bypass(tmp_path):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)
    registry = IssuerRegistry(owner_managed_kinds=(AGENT_DESCRIPTOR_ISSUER_KIND,))
    registry.register(_Issuer())
    registry._adapters.clear()  # lost configured adapter still remains required
    service.bind_issuer_registry(registry, actor_subject=OWNER)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments())
    assert answer["error"] == "issuer_managed_card" and answer["retryable"] is False
    assert await _current(store, control) == control and await _current(store, resident) == resident
    assert decisions.rows == {} and service._persistence.direct == []


@pytest.mark.asyncio
async def test_control_admin_edit_provenance_survives_descriptor_group_refresh(tmp_path):
    service, store, _, resident, control, card_service, _ = await _consent_world(tmp_path)
    edited = replace(control, label="Admin's preset", card_revision=control.card_revision + 1,
                     provenance={AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY: True})
    await card_service.commit(edited, subject_hash=subject_hash_for(OWNER),
                              expected_revision=control.card_revision, now=int(time.time()))
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments())
    assert answer["ok"], answer
    assert (await _current(store, control)).provenance[AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY] is True
    assert (await _current(store, control)).label == "Admin's preset"
    assert (await _current(store, resident)).control_card.control_revision == edited.card_revision + 1


@pytest.mark.asyncio
async def test_missing_credential_is_named_before_mint_or_control_write(tmp_path, monkeypatch):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)
    handles = service._persistence.handles[resident.access_id]
    service._persistence.handles[resident.access_id] = replace(handles, access_token="", session_id="")

    async def mint(*args, **kwargs):
        raise AssertionError("unplanned issuance must not call the ordinary minter")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
    assert answer["error"] == "agent_capability_resident_issuance_not_enlisted" and answer["retryable"]
    assert await _current(store, control) == control and await _current(store, resident) == resident
    assert decisions.rows == {} and service._persistence.direct == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_handle", [False, True])
async def test_expired_bearer_does_not_block_a_narrower_control_decision(tmp_path, monkeypatch, missing_handle):
    from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_PROJECTION_PROPERTY
    from connection_hub.delegated_credentials.controls.effective import effective_card_authority

    service, store, decisions, resident, control, card_service, _ = await _consent_world(tmp_path)
    assert (await _consent(service, resident))["ok"]
    current = await _current(store, resident)
    expired = replace(current, card_revision=current.card_revision + 1, expires_at=int(time.time()) - 10)
    await card_service.commit(expired, subject_hash=subject_hash_for(OWNER),
                              expected_revision=current.card_revision, now=int(time.time()))
    if missing_handle:
        handles = service._persistence.handles[resident.access_id]
        service._persistence.handles[resident.access_id] = replace(handles, access_token="", session_id="")

    async def mint(*args, **kwargs):
        raise AssertionError("Control narrowing never depends on an ordinary mint")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments(allow_post=False))
    assert answer["error"] == "agent_capability_resident_issuance_not_enlisted" and answer["retryable"]
    assert answer["control_committed"] and answer["card_committed"]
    narrowed = await _current(store, control)
    updated = await _current(store, resident)
    projection = effective_card_authority(updated, narrowed).properties[AGENT_CAPABILITY_PROJECTION_PROPERTY]
    assert f"slack/{POST}" not in projection["capabilities"]["named_service_operations"]
    assert updated.expires_at == expired.expires_at and narrowed.card_revision == control.card_revision + 1
    assert decisions.decisions == ["committed", "committed"] and service._persistence.direct == []


@pytest.mark.asyncio
async def test_fresh_participant_recovers_one_control_and_resident_commit_after_finish_cut(tmp_path, monkeypatch):
    from service_foundation.coordination.durable_decision_log import Coordinator
    from connection_hub.delegated_credentials.cards.card_participant import (
        HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
    )

    service, store, decisions, resident, control, card_service, _ = await _consent_world(tmp_path)
    coordinator = service._card_coordinator[0]

    async def cut(transaction_id):
        raise CardServingUnavailable("synthetic_group_finish_cut")

    monkeypatch.setattr(coordinator, "finish", cut)
    answer = await service.sync_agent_capability_control({"user_id": OWNER}, **_refresh_arguments())
    assert answer["ok"] is False and answer["retryable"] and decisions.decisions == ["committed"]
    row = next(iter(decisions.rows.values()))
    assert await tx.list_in_doubt(store)
    intents = LocalCardIntentSource(store, service=card_service, decisions=decisions)
    participant = HubCardParticipant(service=card_service, store=store, intents=intents, decisions=decisions)
    fresh = Coordinator(decisions, {PARTICIPANT: participant}, HubLocalReceiptVerifier(store))
    await fresh.finish(row.transaction_id)
    await fresh.finish(row.transaction_id)
    assert (await _current(store, control)).card_revision == control.card_revision + 1
    updated = await _current(store, resident)
    assert updated.card_revision == resident.card_revision + 1
    assert updated.control_card.control_revision == control.card_revision + 1
    assert decisions.decisions == ["committed"] and await tx.list_in_doubt(store) == []

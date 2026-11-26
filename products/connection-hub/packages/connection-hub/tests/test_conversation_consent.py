"""A consent given in a conversation changes the Agent Card and that conversation's slice in one decision."""

from __future__ import annotations

import time

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore, ConversationSlice
from connection_hub.delegated_credentials.slices.turns import TurnClaims, TurnTokens
from test_agent_capability_control_sync import NAMED_RESOURCE, OWNER
from test_descriptor_agent_card_transaction import _current
from test_descriptor_agent_consent import POST, READ, _consent_world

KEY = b"t" * 32


async def _conversation(service, store, resident, conversation="c1", *, selection=None, card_service=None):
    slices = BundleStorageConversationSliceStore(store._root)
    service.bind_conversation_slices(slices, TurnTokens(KEY))
    if card_service is not None:
        # The production composition (cards/composition.py) passes the same store.
        compose_card_effects(card_service=card_service, card_store=store, grant_store=service._store,
                             policies=None, slices=slices)
    await slices.write(ConversationSlice(
        grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id=conversation, card_generation=resident.created_at,
        base_card_revision=resident.card_revision,
        selection=selection or {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}), expected_revision=0)
    now = int(time.time())
    token = TurnTokens(KEY).issue(TurnClaims(resident.access_id, conversation, "turn-1", now, now + 600, resident.created_at))
    return slices, token


def _consent(service, resident, token, **changes):
    return service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Focused consent",
        client_id=resident.client_id, merge_existing=True, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [POST]}},
        request_id="conversation-consent-1", turn_token=token, **changes)


@pytest.mark.asyncio
async def test_consent_in_a_conversation_commits_card_and_slice_together(tmp_path):
    service, store, decisions, resident, _, card_service, _ = await _consent_world(tmp_path)
    slices, token = await _conversation(service, store, resident, card_service=card_service)
    _, other_token = await _conversation(service, store, resident, "c2")

    answer = await _consent(service, resident, token)
    assert answer["ok"], answer
    assert decisions.decisions == ["committed"]
    saved = await _current(store, resident)
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert current.slice_revision == 2
    assert set(current.selection["named_service_operations"][NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert await slices.prepared_changes(grantor_subject=OWNER, card_access_id=resident.access_id,
                                         conversation_id="c1") == []
    # Another conversation keeps its own choice.
    other = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c2")
    assert other.slice_revision == 1 and other.selection["named_service_operations"][NAMED_RESOURCE]["slack"] == [READ]


@pytest.mark.asyncio
async def test_a_consent_naming_a_foreign_or_unseeded_conversation_commits_nothing(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    await _conversation(service, store, resident)

    forged = TurnTokens(b"x" * 32).issue(TurnClaims(resident.access_id, "c1", "turn-1", 0, int(time.time()) + 600,
                                                    resident.created_at))
    refused = await _consent(service, resident, forged)
    assert refused["ok"] is False and refused["error"] == "turn_token_invalid", refused

    now = int(time.time())
    unseeded = TurnTokens(KEY).issue(TurnClaims(resident.access_id, "never-seeded", "turn-1", now, now + 600,
                                                 resident.created_at))
    refused = await _consent(service, resident, unseeded)
    assert refused["ok"] is False and refused["error"] == "conversation_slice_missing", refused
    assert decisions.decisions == []
    assert (await _current(store, resident)).card_revision == resident.card_revision


@pytest.mark.asyncio
async def test_a_crashed_consent_never_blocks_calls_and_the_recovery_cron_releases_it(tmp_path):
    from connection_hub.delegated_credentials.live_grant import ResolvedCardComposition
    from connection_hub.delegated_credentials.slices import guard

    service, store, decisions, resident, control, card_service, _ = await _consent_world(tmp_path)
    slices, token = await _conversation(service, store, resident, card_service=card_service)
    decide = decisions.decide

    async def crash(*args, **kwargs):
        raise RuntimeError("process died before the decision")

    decisions.decide = crash
    with pytest.raises(RuntimeError, match="process died"):
        await _consent(service, resident, token)
    assert await slices.prepared_changes(grantor_subject=OWNER, card_access_id=resident.access_id,
                                         conversation_id="c1")

    # A-2: a call meanwhile is not refused; it gets committed AND prepared (the
    # consent widens, so the committed READ-only slice applies).
    # The undecided transaction also fences the Agent Card itself (pending effects
    # keep read resolution fenced), as for any Card transaction; the
    # guard's own composition is checked here on the last committed Card.
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    with pytest.raises(CardStorageError, match="card_transaction_undecided"):
        await _current(store, resident)
    from types import SimpleNamespace
    readers = SimpleNamespace(turn_tokens=TurnTokens(KEY), conversation_registry=None)
    if True:
        card = resident
        composition = ResolvedCardComposition(caller_card=card, control_card=control, effective_card=card)
        narrowed = await guard.narrow_live_composition_for_turn(composition, card_store=store, turn_token=token,
                                                                resolvers=readers)
        assert set(narrowed.effective_card.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ}

    # The Hub's recovery cron presumes ABORT (the test store's expiry CAS) and
    # FINISH releases the slice marker; neither the Card nor the slice moved.
    decisions.decide = decide
    # The cron's recover_card_transactions pages the production store; this test
    # store has no paging, so it drives the same recovery rows (_recover_rows).
    await service._card_coordinator[0].recover(limit=10)
    assert await slices.prepared_changes(grantor_subject=OWNER, card_access_id=resident.access_id,
                                         conversation_id="c1") == []
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert current.slice_revision == 1
    assert (await _current(store, resident)).card_revision == resident.card_revision


@pytest.mark.asyncio
async def test_the_slice_gains_the_bridge_tools_of_the_consented_operation(tmp_path):
    service, store, decisions, resident, _, card_service, _ = await _consent_world(tmp_path)
    # This conversation had narrowed the named-service bridge away.
    slices, token = await _conversation(service, store, resident, card_service=card_service, selection={
        "named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}},
        "resource_operations": {NAMED_RESOURCE: []}})
    answer = await _consent(service, resident, token)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert set(saved.resource_operations[NAMED_RESOURCE]) <= set(current.selection["resource_operations"][NAMED_RESOURCE])
    assert "named_services_call" in current.selection["resource_operations"][NAMED_RESOURCE]


@pytest.mark.asyncio
async def test_a_consent_in_a_conversation_also_becomes_the_cards_default(tmp_path):
    """Design: consent writes the Agent Card (the new default); new conversations and headless calls get it."""
    from connection_hub.delegated_credentials.slices.guard import card_default_selection

    service, store, decisions, resident, _, card_service, _ = await _consent_world(tmp_path)
    _, token = await _conversation(service, store, resident, card_service=card_service)

    answer = await _consent(service, resident, token)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    defaults = card_default_selection(saved)
    assert defaults is not None
    assert POST in defaults["named_service_operations"][NAMED_RESOURCE]["slack"]

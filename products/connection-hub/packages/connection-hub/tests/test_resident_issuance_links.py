"""Resident/Control issuance plans adopt their existing hidden Card files once."""

from dataclasses import replace
from datetime import datetime, timezone
import time

import pytest

from service_foundation.coordination.durable_decision_log import IntentDraft
from connection_hub.delegated_credentials.cards.card_group import group_member, hub_group_participant_input
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT, LocalCardIntentSource
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.cards.version_link import pointer_link, staging_tag, write_hidden_version
from test_card_coordinated_writes import _host
from test_descriptor_agent_card_save import _descriptor_pair


async def _planned_group(tmp_path):
    host, store, decisions, _, _ = await _host(tmp_path)
    host._persistence.card_store = store
    card_service = host._card_coordinator[0].participants[PARTICIPANT]._service
    _, resident, control = await _descriptor_pair()
    originals = sorted((resident, control), key=lambda card: card.access_id)
    for card in originals:
        await card_service.commit(card, subject_hash=subject_hash_for(card.grantor_subject),
                                  expected_revision=0, now=int(time.time()))
    candidates = [replace(resident, card_revision=resident.card_revision + 1,
        control_card=replace(resident.control_card, control_revision=control.card_revision + 1)),
        replace(control, card_revision=control.card_revision + 1, label="Refreshed Control")]
    candidates.sort(key=lambda card: card.access_id)
    request = "a" * 64
    tag = staging_tag("oauth-issuance-candidate", request)
    members = []
    at = datetime.now(timezone.utc)
    for before, candidate in zip(originals, candidates):
        subject_hash = subject_hash_for(before.grantor_subject)
        pointer, _ = await store.read_current_authority(subject_hash=subject_hash, access_id=before.access_id)
        link = await write_hidden_version(store, subject_hash=subject_hash, authority=candidate, at=at, tag=tag)
        members.append({"subject_hash": subject_hash, "access_id": before.access_id,
            "original": pointer_link(pointer), "candidate": link, "action": "update"})
    reads = ()
    catalog = "e" * 64
    projection = hub_group_participant_input(members=[group_member(original=before, candidate=after, action="update")
        for before, after in zip(originals, candidates)], actor_subject=resident.grantor_subject,
        actor_kind="caller", reads=reads, catalog_version_digest=catalog)
    row = await decisions.begin(IntentDraft(replay_scope="synthetic-resident-links", request_id=request,
        expires_at=int(time.time()) + 60, participants=(PARTICIPANT,),
        payload={"participant_inputs": {PARTICIPANT: projection}}))
    plan = {"decision_request_id": request, "access_id": resident.access_id,
        "intent": {"shape": "group", **members[0], "members": members, "effects": [], "reads": reads,
            "catalog": catalog, "actor_subject": resident.grantor_subject, "actor_kind": "caller"}}
    return host, store, decisions, card_service, row, plan, candidates


@pytest.mark.asyncio
async def test_multi_member_plan_reconstructs_and_adopts_the_same_card_links(tmp_path):
    host, store, decisions, cards, row, plan, candidates = await _planned_group(tmp_path)
    intent = await host._issuance_intent(plan, row.transaction_id, row.intent.digest)
    assert [member.candidate for member in intent.members] == candidates
    assert intent.catalog == "e" * 64
    source = LocalCardIntentSource(store, service=cards, decisions=decisions)
    await source.record(intent)
    reconstructed = await host._issuance_intent(plan, row.transaction_id, row.intent.digest)
    await source.record(reconstructed)
    reloaded = await source.load(row.transaction_id)
    assert [member.candidate for member in reloaded.members] == candidates
    assert [member.candidate_link for member in reconstructed.members] == [
        member["candidate"] for member in plan["intent"]["members"]]
    # A request cleanup cannot delete files the decision adopted, including its later Control member.
    recorded = {"members": [{"link": member["candidate"], "subject_hash": member["subject_hash"],
        "access_id": member["access_id"]} for member in plan["intent"]["members"]]}
    assert await host._discard_planned_candidate(recorded, plan["decision_request_id"]) is False
    assert [member.candidate for member in (await source.load(row.transaction_id)).members] == candidates


@pytest.mark.asyncio
async def test_request_cleanup_releases_every_unadopted_member_file(tmp_path):
    host, _, _, _, _, plan, _ = await _planned_group(tmp_path)
    recorded = {"members": [{"link": member["candidate"], "subject_hash": member["subject_hash"],
        "access_id": member["access_id"]} for member in plan["intent"]["members"]]}
    assert await host._discard_planned_candidate(recorded, plan["decision_request_id"]) is True
    assert await host._discard_planned_candidate(recorded, plan["decision_request_id"]) is False

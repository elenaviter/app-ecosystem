"""W41 migration: per-user descriptor Control copies to the one shared Control.

Dry run reads only and lists each copy and how it differs; apply rebinds each
Agent Card still on its owner's copy to the shared Control, then revokes the
copy; a second apply finds nothing (T10, T11). An administrator's override on
their own copy is listed, never carried over.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.agent_capability_control import AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.descriptor_control_migration import migrate_descriptor_controls
from test_agent_capability_control_sync import OWNER, RESOURCE
from test_descriptor_agent_card_transaction import _current, _world


def card_service_of(service):
    from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
    return service._card_coordinator[0].participants[PARTICIPANT]._service


async def _with_legacy_copy(tmp_path, *, edited=False):
    service, store, decisions, resident, shared, card_service, _ = await _world(tmp_path)
    legacy_id = control_card_id_for_issuer("kdcube_agent_descriptor", RESOURCE, grantor_subject=OWNER)
    copy = replace(shared, access_id=legacy_id, grantor_subject=OWNER, card_revision=1,
                   label="Administrator's own copy" if edited else shared.label,
                   provenance={AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY: True} if edited else {})
    await card_service.commit(copy, subject_hash=subject_hash_for(OWNER), expected_revision=0, now=int(time.time()))
    # The fixture's persistence serves handles per Card id; a Control has empty ones.
    service._persistence.handles[legacy_id] = service._persistence.handles[shared.access_id]
    legacy_card = replace(resident, card_revision=resident.card_revision + 1, control_card=replace(
        resident.control_card, control_id=legacy_id, holder_subject="", control_revision=copy.card_revision))
    await card_service.commit(legacy_card, subject_hash=subject_hash_for(OWNER),
                              expected_revision=resident.card_revision, now=int(time.time()))
    return service, store, legacy_card, copy, shared


@pytest.mark.asyncio
async def test_a_dry_run_lists_each_copy_and_changes_nothing(tmp_path):
    service, store, card, copy, shared = await _with_legacy_copy(tmp_path, edited=True)

    report = await migrate_descriptor_controls(service, store, apply=False)

    assert report["applied"] is False and report["per_user_copies"] == 1 and report["shared_controls"] == 1
    assert report["administrator_edited_copies"] == 1
    [row] = report["copies"]
    assert row == {"agent": RESOURCE, "owner": OWNER, "control_id": copy.access_id, "administrator_edited": True,
                   "differs_from_shared": [], "bound_agent_cards": 1}
    assert await _current(store, card) == card and await _current(store, copy) == copy


@pytest.mark.asyncio
async def test_apply_rebinds_the_card_revokes_the_copy_and_is_idempotent(tmp_path):
    service, store, card, copy, shared = await _with_legacy_copy(tmp_path, edited=True)
    revoked = []

    async def revoke(user, *, control_id):
        # The revocation itself is the ordinary Card lifecycle (tested there);
        # this fixture's persistence does not model it.
        revoked.append((user, control_id))
        current = await _current(store, copy)
        from connection_hub.delegated_credentials.cards.store import subject_hash_for as hash_for
        await card_service_of(service).commit(replace(current, card_revision=current.card_revision + 1,
                                                      state="revoked"),
                                              subject_hash=hash_for(OWNER), expected_revision=current.card_revision,
                                              now=int(time.time()))
        return {"ok": True}

    service.control_card_revoke = revoke

    report = await migrate_descriptor_controls(service, store, apply=True)
    assert revoked == [({"user_id": OWNER}, copy.access_id)]

    assert report["counts"] == {"agent_cards_rebound": 1, "copies_revoked": 1}, report
    rebound = await _current(store, card)
    assert (rebound.control_card.holder_subject, rebound.control_card.control_id) == (
        shared.grantor_subject, shared.access_id)
    assert rebound.control_card.control_revision == shared.card_revision
    assert (await _current(store, copy)).state != "active"
    # The administrator's override was not carried over: the shared Control is unchanged.
    assert await _current(store, shared) == shared

    again = await migrate_descriptor_controls(service, store, apply=True)
    assert again["per_user_copies"] == 0 and again["counts"] == {}

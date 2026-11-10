# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W35: a connector Card keeps its entry door on every save, and a revision without it is refused.

A connector Card's identity is (owner, client, kind=connector, entry_resource). The Card editor's Save
(``update_access``) rebuilt the next revision without ``entry_resource``, so any Save, even a rename,
emptied it. A refresh family issued by the original exchange records only ``resources``; its refresh
takes the resource from the Card, found none, and ``record_oauth_grant`` refused it for good with
``card_identity_entry_resource_missing`` (live on yaroslav-linux, 2026-11-10, Card
oauth-ec4159216b2ff409: rev 2 after a rename, refresh refused at 16:54Z).

Covered here: the editor Save path keeps the door, and the composed issue, Save, refresh sequence
succeeds; every durable write gate (ordinary commit, staged transaction, Card version) refuses a live
connector revision without its door, while a revoke stays possible for a Card already damaged.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import model as card_model
from connection_hub.delegated_credentials.cards import service as card_service_module
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_REVOKED,
    CONNECTOR_ENTRY_RESOURCE_MISSING,
    connector_entry_resource_refusal,
)
from connection_hub.delegated_credentials.cards.service import CardConflict, DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from test_card_service import NOW, SUBJECT_HASH, _authority, _Cache
from test_card_transaction_store import INTENT, TX, Decisions
from test_resident_profile_cards import GRANTOR, _Harness
from test_w661_card_versions import DIGEST, WHEN

ENTRY = "https://example.test/mcp"
CLIENT = "dcr-claude-code"
CONCRETE = "https://host/api/mcp/memories"
USER = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}


def _connector(**changes):
    values = {"card_kind": CARD_KIND_CONNECTOR, "client_id": CLIENT, "entry_resource": ENTRY, **changes}
    return replace(_authority(), **values)


@asynccontextmanager
async def _no_lock(**kwargs):
    yield


async def _card_service(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_no_lock)
    before = _connector()
    await service.commit(before, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    return store, service, before


async def _current(store, card):
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    return None if found is None else found[1]


# --- the rule itself ------------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["", "   "])
def test_a_live_connector_without_its_door_is_refused(entry):
    assert connector_entry_resource_refusal(_connector(entry_resource=entry)) == CONNECTOR_ENTRY_RESOURCE_MISSING


def test_a_door_a_revoked_revision_and_other_kinds_pass():
    assert connector_entry_resource_refusal(_connector()) is None
    assert connector_entry_resource_refusal(_connector(entry_resource="", state=CARD_STATE_REVOKED)) is None
    assert connector_entry_resource_refusal(_authority()) is None  # an automation Card has no door


# --- every durable write gate ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_refuses_a_connector_revision_that_drops_its_door(tmp_path):
    store, service, before = await _card_service(tmp_path)
    emptied = replace(before, card_revision=before.card_revision + 1, label="renamed", entry_resource="")

    with pytest.raises(CardConflict, match=CONNECTOR_ENTRY_RESOURCE_MISSING):
        await service.commit(emptied, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)

    assert await _current(store, before) == before
    kept = replace(before, card_revision=before.card_revision + 1, label="renamed")
    await service.commit(kept, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    assert (await _current(store, before)).entry_resource == ENTRY


@pytest.mark.asyncio
async def test_a_staged_transaction_refuses_a_connector_candidate_without_its_door(tmp_path):
    store, _service, before = await _card_service(tmp_path)
    emptied = replace(before, card_revision=before.card_revision + 1, label="renamed", entry_resource="")

    with pytest.raises(tx.CardTransactionRefused, match=CONNECTOR_ENTRY_RESOURCE_MISSING):
        await tx.stage(store, transaction_id=TX, intent_digest=INTENT, participant="project",
                       subject_hash=SUBJECT_HASH, original=before, candidate=emptied, now=WHEN)

    assert await tx.read_receipt(store, TX) is None
    assert await _current(store, before) == before


@pytest.mark.asyncio
async def test_a_card_version_refuses_a_connector_member_without_its_door(tmp_path):
    store, service, before = await _card_service(tmp_path)
    emptied = replace(before, card_revision=before.card_revision + 1, label="renamed", entry_resource="")

    with pytest.raises(tx.CardTransactionRefused, match=CONNECTOR_ENTRY_RESOURCE_MISSING):
        await service.stage_card_version(txn="w35-txn-" + "a" * 32, request_digest=DIGEST, catalog="catalog-1",
                                         members=[(SUBJECT_HASH, before.access_id, 1, emptied)], now=WHEN)

    assert await _current(store, before) == before


@pytest.mark.asyncio
async def test_a_card_already_damaged_can_still_be_revoked(tmp_path, monkeypatch):
    store, service, before = await _card_service(tmp_path)
    damaged = replace(before, card_revision=before.card_revision + 1, entry_resource="")
    # A revision written before this gate existed (the live Cards of 2026-11-05 to 11-10).
    monkeypatch.setattr(card_service_module, "connector_entry_resource_refusal", lambda authority: None)
    await service.commit(damaged, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    monkeypatch.setattr(card_service_module, "connector_entry_resource_refusal",
                        card_model.connector_entry_resource_refusal)

    await service.revoke(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                         expected_revision=damaged.card_revision)

    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert found is None or found[1].state == CARD_STATE_REVOKED


# --- the editor Save path, composed with the refresh that broke -----------------------------------


async def _issued(harness):
    """The original exchange: a connector Card whose refresh family records no single resource."""
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Claude Code",
        scopes=["memories:read"], resource=CONCRETE, access_token="at-1", refresh_token="rt-1",
    )
    assert record is not None
    card = await harness.card(record.access_id)
    assert card.card_kind == CARD_KIND_CONNECTOR and card.entry_resource == CONCRETE
    return record, card


async def _editor_rename(harness, record, card):
    """The editor's Save of an unchanged selection with a new name: the reported reproduction."""
    return await harness.service.update_access(
        USER, access_id=record.access_id, label="Renamed",
        resource_grants={key: list(value) for key, value in card.resource_grants.items()},
        resource_operations={key: list(value) for key, value in card.resource_operations.items()},
    )


@pytest.mark.asyncio
async def test_an_editor_rename_keeps_the_entry_door(tmp_path):
    harness = _Harness(tmp_path)
    record, issued = await _issued(harness)

    result = await _editor_rename(harness, record, issued)

    assert result.get("ok") is not False, result
    saved = await harness.card(record.access_id)
    assert saved.label == "Renamed"
    assert saved.card_revision == issued.card_revision + 1
    assert saved.entry_resource == CONCRETE


@pytest.mark.asyncio
async def test_issue_then_editor_save_then_refresh_succeeds(tmp_path):
    harness = _Harness(tmp_path)
    record, issued = await _issued(harness)
    result = await _editor_rename(harness, record, issued)
    assert result.get("ok") is not False, result

    # The SDK refresh (routes.py, W929): the family has no "resource", so the Card's door is used.
    saved = await harness.card(record.access_id)
    refreshed = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Claude Code",
        scopes=["memories:read"], resource=saved.entry_resource, access_token="at-2", refresh_token="rt-2",
    )

    assert refreshed is not None
    assert refreshed.access_id == record.access_id
    assert (await harness.card(record.access_id)).entry_resource == CONCRETE

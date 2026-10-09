"""W676: a refresh rotation committed under Card transactions moves the Card's handle row with it.

Live (dev-main, 2026-10-09 17:39Z): a refresh of a Control-bound OAuth Card committed Card revision 22 while
its handle row stayed at 21. The Hub bound Card transactions without the Card handle store, so the writer
pinned no ``handle_binding`` effect and every later strict handle read of that Card refused
(card_handle_revision_mismatch); the owner's listing then failed for all his agents. The composition now
binds the persistence's own handle store when none is passed, so the writer pins the row and the effect
applier can move it at COMMIT.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite
from connection_hub.delegated_credentials.cards.composition import bind_card_transactions
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from test_card_transaction_store import _Applier, _setup, _visible
from test_w578_bound_card_writes import _bound


class _HandleRow:
    """A handle store whose one active row is pinned at the Card's current revision and expiry."""

    def __init__(self, card):
        self.row = {"from_identity": "row-identity-1", "from_fingerprint": "",
                    "from_revision": card.card_revision, "from_expires_at": card.expires_at}

    async def binding_identity(self, access_id):
        return dict(self.row)


def _rotation(card):
    rotated = replace(card, card_revision=card.card_revision + 1, expires_at=card.expires_at + 3600)
    return rotated, record_from_card(rotated, CardCredentialHandles(
        access_id=card.access_id, access_token="rotated-access-token", refresh_token="rotated-refresh-token"))


@pytest.mark.asyncio
async def test_binding_card_transactions_binds_the_persistences_handle_store(tmp_path):
    store, card_service, _, _ = await _setup(tmp_path)
    handles = object()
    host = object.__new__(AutomationAccessService)
    persistence = SimpleNamespace(card_store=store, card_service=card_service, credential_handles=handles)
    bind_card_transactions(host, persistence=persistence, decisions=object(), grant_store=None, policies=None)
    assert host._card_credential_handles is handles


@pytest.mark.asyncio
async def test_an_explicit_handle_store_still_wins(tmp_path):
    store, card_service, _, _ = await _setup(tmp_path)
    explicit, own = object(), object()
    host = object.__new__(AutomationAccessService)
    persistence = SimpleNamespace(card_store=store, card_service=card_service, credential_handles=own)
    bind_card_transactions(host, persistence=persistence, decisions=object(), grant_store=None, policies=None,
                           credential_handles=explicit)
    assert host._card_credential_handles is explicit


def test_the_durable_persistence_exposes_its_handle_store():
    from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence

    assert isinstance(DurableCardPersistence.credential_handles, property)


class _Recording:
    """The effect applier, recording each effect's kind and payload as COMMIT applies it."""

    def __init__(self):
        self.applied = []

    async def __call__(self, kind, key, payload, *, transaction_id):
        self.applied.append((kind, dict(payload)))


@pytest.mark.asyncio
async def test_a_refresh_rotation_carries_the_handle_binding_to_the_new_revision_and_expiry(tmp_path):
    host, store, bound = await _bound(tmp_path)
    host.bind_card_credential_handles(_HandleRow(bound))
    applier = _Recording()
    host._card_coordinator[0].participants["connection-hub.card"]._service.bind_effect_applier(applier)
    rotated, record = _rotation(bound)
    await host._persist_record(record, expected_revision=bound.card_revision,
                               caller_write=CallerWrite("oauth_grant", "person-1"))
    assert await _visible(store, bound) == rotated
    assert [kind for kind, _ in applier.applied] == ["handle_binding"]
    payload = applier.applied[0][1]
    assert (payload["access_id"], payload["from_revision"], payload["from_expires_at"]) == (
        bound.access_id, bound.card_revision, bound.expires_at)
    assert (payload["card_revision"], payload["expires_at"]) == (rotated.card_revision, rotated.expires_at)
    assert "rotated-access-token" not in repr(applier.applied) and "rotated-refresh-token" not in repr(applier.applied)


@pytest.mark.asyncio
async def test_without_a_bound_handle_store_the_rotation_moves_no_row_the_live_defect(tmp_path):
    host, store, bound = await _bound(tmp_path)
    applier = _Applier()
    host._card_coordinator[0].participants["connection-hub.card"]._service.bind_effect_applier(applier)
    _, record = _rotation(bound)
    await host._persist_record(record, expected_revision=bound.card_revision,
                               caller_write=CallerWrite("oauth_grant", "person-1"))
    assert [kind for _, kind, _ in applier.applied] == []  # the Card moved, its handle row would not

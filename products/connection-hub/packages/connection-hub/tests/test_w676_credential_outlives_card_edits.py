"""A credential works until its own revocation or expiry, or the Card's revocation; a Card edit never breaks it.

Operator, 2026-10-09: "changing something on teh card does not change the credential. credential is changed
separately by refresh, revoke etc" / "this token must work as long as it is not revoke or expired. and even
if i changed the card it still must work unchanged" / "revoking the Card also invalidate the credential, of
course." / "simply make wha twas added undo". Live: a refresh committed Card revision 22 while its handle
row stayed at 21, and the exact revision check added on 2026-09-23 (a5f57d93) then refused every read of that
Card. That check is undone for reads: a read checks only that the row is this Card's. The Card's revocation
is enforced on the live Card at use (test_w652_live_authority_readers.py).
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from connection_hub.delegated_credentials.cards.credential_handles import (
    CardCredentialHandleUnavailable, PostgresCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE, HANDLE_STATE_REVOKED, CardHandleMetadata,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT, CARD_KIND_AUTOMATION
from test_credentialless_card_persistence import _agent

NOW = int(time.time())


class _Metadata:
    def __init__(self, row=None):
        self.row = row

    async def read_active(self, access_id, *, now=None):
        row = self.row
        if row is None or row.access_id != access_id or row.state != HANDLE_STATE_ACTIVE or row.expires_at <= NOW:
            return None
        return row

    async def read_current(self, access_id):
        return self.row if self.row is not None and self.row.access_id == access_id else None

    async def put(self, metadata, *, expected_revision):
        current_revision = self.row.revision if self.row is not None else 0
        if current_revision != expected_revision:
            raise RuntimeError("card_handle_metadata_conflict")
        self.row = dataclasses.replace(metadata, revision=current_revision + 1)
        self.puts = getattr(self, "puts", 0) + 1
        return self.row


class _Resident:
    def __init__(self):
        self.resolved = []

    async def resolve(self, access_id, *, now=None):
        self.resolved.append(access_id)
        return "resident-bearer-of-" + access_id


def _card(kind, *, revision, expires_at, state="active"):
    base = _agent()
    access_id = "agent-card-1" if kind == CARD_KIND_AGENT else "oauth-card-1"
    return dataclasses.replace(base, access_id=access_id, card_kind=kind, card_revision=revision,
                               expires_at=expires_at, state=state)


def _row(card, *, revision, expires_at, state=HANDLE_STATE_ACTIVE, resident=False):
    return CardHandleMetadata(access_id=card.access_id, card_revision=revision, expires_at=expires_at,
                              resident_access_secret_ref="ref-1" if resident else "",
                              resident_access_sha256=("a" * 64) if resident else "", state=state).validated()


def _store(row):
    resident = _Resident()
    return PostgresCardCredentialHandleStore(metadata_store=_Metadata(row), resident_secrets=resident), resident


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["read", "read_current"])
async def test_an_edited_or_refreshed_oauth_card_still_loads_its_credential(read):
    # The handle row was written at revision 21; edits and refreshes moved the Card to 22 with a later expiry.
    card = _card(CARD_KIND_AUTOMATION, revision=22, expires_at=NOW + 7200)
    store, resident = _store(_row(card, revision=21, expires_at=NOW + 3600))
    handles = await getattr(store, read)(card)
    assert handles.access_id == card.access_id and resident.resolved == []


@pytest.mark.asyncio
async def test_a_card_issued_without_a_row_gets_its_row_on_first_read_and_keeps_working():
    # A Card the original exchange issued before issue-time rows (e7b3c18a live) has NO row: its first read
    # writes it from the current Card, exactly as issue does now, and later reads use that row.
    card = _card(CARD_KIND_AUTOMATION, revision=1, expires_at=NOW + 7200)
    store, resident = _store(None)
    assert (await store.read(card)).access_id == card.access_id
    row = store._metadata.row
    assert (row.access_id, row.card_revision, row.expires_at, row.state) == (
        card.access_id, card.card_revision, card.expires_at, HANDLE_STATE_ACTIVE)
    edited = dataclasses.replace(card, card_revision=2)
    assert (await store.read(edited)).access_id == card.access_id  # a later edit still reads, no rewrite
    assert store._metadata.puts == 1 and resident.resolved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", ["revoked", "expired"])
async def test_an_ended_credential_row_is_never_recreated(ended):
    card = _card(CARD_KIND_AUTOMATION, revision=1, expires_at=NOW + 7200)
    row = _row(card, revision=1, expires_at=(NOW - 10 if ended == "expired" else NOW + 3600),
               state=HANDLE_STATE_REVOKED if ended == "revoked" else HANDLE_STATE_ACTIVE)
    store, _ = _store(row)
    with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_metadata_missing"):
        await store.read(card)
    assert getattr(store._metadata, "puts", 0) == 0


@pytest.mark.asyncio
async def test_an_agent_card_without_its_row_is_refused_never_created():
    card = _card(CARD_KIND_AGENT, revision=1, expires_at=NOW + 7200)
    store, resident = _store(None)
    with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_metadata_missing"):
        await store.read(card)
    assert store._metadata.row is None and resident.resolved == []


@pytest.mark.asyncio
async def test_two_first_reads_racing_both_succeed_with_one_row():
    import asyncio

    card = _card(CARD_KIND_AUTOMATION, revision=1, expires_at=NOW + 7200)
    store, _ = _store(None)
    real_read_current = store._metadata.read_current

    async def both_see_no_row(access_id):
        value = await real_read_current(access_id)
        await asyncio.sleep(0)
        return value

    store._metadata.read_current = both_see_no_row
    results = await asyncio.gather(store.read(card), store.read(card))
    assert [handles.access_id for handles in results] == [card.access_id, card.access_id]
    assert store._metadata.puts == 1


@pytest.mark.asyncio
async def test_the_handle_store_reaches_only_the_issuance_effect_never_the_card_writer():
    from types import SimpleNamespace

    from connection_hub.delegated_credentials.cards import composition, effect_targets

    class _Service:
        def __init__(self):
            self.handle_store_bound = None

        def bind_card_coordinator(self, coordinator, *, intents, decisions):
            pass

        def bind_oauth_issuance_store(self, store):
            pass

        def bind_card_credential_handles(self, handles):
            self.handle_store_bound = handles

    class _CardService:
        def bind_effect_applier(self, applier):
            self.apply = applier

        def bind_effect_preparer(self, preparer):
            self.prepare = preparer

        def bind_effect_releaser(self, releaser):
            self.release = releaser

    class _CardStore:
        root = None

    handles, issuance = object(), object()
    service, cards = _Service(), _CardService()
    composition.bind_card_transactions(
        service, persistence=SimpleNamespace(card_store=_CardStore(), card_service=cards), decisions=object(),
        grant_store=object(), policies=None, issuance_store=issuance, issue_credential_handles=handles)
    targets = cards.prepare.__self__._targets
    assert isinstance(targets["credential_issue"], effect_targets.CredentialIssueTarget)
    assert targets["credential_issue"]._credential_handles is handles  # a new Card's row is written at issue
    assert service.handle_store_bound is None  # a Card edit plans no handle effect: the credential never moves


@pytest.mark.asyncio
async def test_an_edited_agent_card_still_resolves_its_resident_bearer():
    card = _card(CARD_KIND_AGENT, revision=22, expires_at=NOW + 7200)
    store, resident = _store(_row(card, revision=21, expires_at=NOW + 3600, resident=True))
    handles = await store.read(card)
    assert handles.access_token == "resident-bearer-of-agent-card-1" and resident.resolved == [card.access_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("row_state", ["missing", "revoked", "expired"])
async def test_an_agent_credential_ends_with_its_own_revocation_or_expiry(row_state):
    card = _card(CARD_KIND_AGENT, revision=22, expires_at=NOW + 7200)
    row = None if row_state == "missing" else _row(
        card, revision=22, expires_at=(NOW - 10 if row_state == "expired" else NOW + 3600),
        state=HANDLE_STATE_REVOKED if row_state == "revoked" else HANDLE_STATE_ACTIVE, resident=True)
    store, resident = _store(row)
    with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_metadata_missing"):
        await store.read(card)
    assert resident.resolved == []


@pytest.mark.asyncio
async def test_a_row_of_another_card_is_never_accepted():
    card = _card(CARD_KIND_AGENT, revision=22, expires_at=NOW + 7200)
    other = dataclasses.replace(card, access_id="another-card")
    store, resident = _store(_row(other, revision=22, expires_at=NOW + 3600, resident=True))
    store._metadata.read_active = lambda access_id, now=None: _async(_row(other, revision=22,
                                                                           expires_at=NOW + 3600, resident=True))
    with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_access_id_mismatch"):
        await store.read(card)
    assert resident.resolved == []


def test_migration_reconciliation_keeps_the_exact_revision_binding():
    card = _card(CARD_KIND_AUTOMATION, revision=22, expires_at=NOW + 7200)
    with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_revision_mismatch"):
        PostgresCardCredentialHandleStore.validate_migration_binding(
            card, _row(card, revision=21, expires_at=NOW + 7200))


async def _async(value):
    return value

"""A credential works until its own revocation or expiry, or the Card's revocation; a Card edit never breaks it.

Operator, 2026-10-09: "changing something on teh card does not change the credential. credential is changed
separately by refresh, revoke etc" / "this token must work as long as it is not revoke or expired. and even
if i changed the card it still must work unchanged" / "revoking the Card also invalidate the credential, of
course." Live: a refresh committed Card revision 22 while its handle row stayed at 21, and the exact
revision check then refused every read of that Card. A read now checks only that the row is this Card's.
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
@pytest.mark.parametrize("row_state", ["missing", "revoked", "expired"])
async def test_an_oauth_card_needs_no_handle_row_its_credential_lives_with_the_oauth_authority(row_state):
    card = _card(CARD_KIND_AUTOMATION, revision=22, expires_at=NOW + 7200)
    row = None if row_state == "missing" else _row(
        card, revision=21, expires_at=(NOW - 10 if row_state == "expired" else NOW + 3600),
        state=HANDLE_STATE_REVOKED if row_state == "revoked" else HANDLE_STATE_ACTIVE)
    store, _ = _store(row)
    assert (await store.read(card)).access_id == card.access_id


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

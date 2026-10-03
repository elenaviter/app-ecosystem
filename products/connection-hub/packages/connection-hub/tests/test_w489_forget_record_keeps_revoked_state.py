"""W489: a revoked revision handed to ``_forget_record`` must reach the Card
service as revoked.

On 2026-10-03 every sign-in of an invited person failed with
``project_invitation_binding_claim_conflict``. The claim built the revoked
pending Card correctly, but ``_forget_record`` rebuilt it from a record, and
``card_authority_from_record`` always yields an active authority. The Card
service compares the supplied revision with ``replace_state(current,
revoked)`` and refused it as ``revoked_authority_invalid``. The lifecycle
tests replace ``_forget_record`` with a fake, so they never met the real check.
The same path serves invitation withdrawal, person removal and identity
lifecycle revocations.
"""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    _subject_key,
    card_authority_from_record,
    record_from_card,
)
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.service import (
    CardConflict,
    DelegatedCardService,
    replace_state,
)
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

from test_card_service import NOW, _authority, _Cache


class _Cards:
    """The persistence facade's ``forget``, over the real Card service."""

    def __init__(self, service: DelegatedCardService) -> None:
        self._service = service

    async def forget(self, authority, *, subject_hash, revoked_authority=None):
        await self._service.revoke(
            subject_hash=subject_hash,
            access_id=authority.access_id,
            expected_revision=authority.card_revision,
            revoked_authority=revoked_authority,
        )


class _Host:
    """Only what ``AutomationAccessService._forget_record`` reads."""

    def __init__(self, cards: _Cards) -> None:
        self._cards_facade = cards

    def _cards(self) -> _Cards:
        return self._cards_facade


async def _committed(tmp_path):
    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield {"owner": "test"}

    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    authority = _authority()
    subject_hash = _subject_key(authority.grantor_subject)
    await service.commit(authority, subject_hash=subject_hash, expected_revision=0, now=NOW)
    return store, service, authority, subject_hash


@pytest.mark.asyncio
async def test_forget_record_commits_an_audited_revoked_revision(tmp_path) -> None:
    store, service, authority, subject_hash = await _committed(tmp_path)
    audit = {"project_invitation_binding": {"request_id": "req-1"}}
    revoked = dataclasses.replace(
        replace_state(authority, CARD_STATE_REVOKED),
        provenance={**dict(authority.provenance or {}), **audit},
    )

    await AutomationAccessService._forget_record(
        _Host(_Cards(service)),
        record_from_card(authority),
        revoked_record=record_from_card(revoked),
    )

    current = await store.read_current_authority(
        subject_hash=subject_hash, access_id=authority.access_id
    )
    assert current is not None
    assert current[1].state == CARD_STATE_REVOKED
    assert current[1].card_revision == authority.card_revision + 1
    assert current[1].provenance["project_invitation_binding"] == {"request_id": "req-1"}


@pytest.mark.asyncio
async def test_a_record_round_trip_alone_loses_the_revoked_state(tmp_path) -> None:
    """Why the fix lives in ``_forget_record``: the record shape has no state."""

    _store, service, authority, subject_hash = await _committed(tmp_path)
    revoked = replace_state(authority, CARD_STATE_REVOKED)
    assert card_authority_from_record(record_from_card(revoked)).state != CARD_STATE_REVOKED
    with pytest.raises(CardConflict, match="revoked_authority_invalid"):
        await service.revoke(
            subject_hash=subject_hash,
            access_id=authority.access_id,
            expected_revision=authority.card_revision,
            revoked_authority=card_authority_from_record(record_from_card(revoked)),
        )


@pytest.mark.asyncio
async def test_forget_record_still_refuses_a_revision_that_changes_authority(tmp_path) -> None:
    """The fix restores the state only; any other change is still refused."""

    store, service, authority, subject_hash = await _committed(tmp_path)
    rewritten = dataclasses.replace(
        replace_state(authority, CARD_STATE_REVOKED), label="Rewritten while revoking"
    )

    with pytest.raises(CardConflict, match="revoked_authority_invalid"):
        await AutomationAccessService._forget_record(
            _Host(_Cards(service)),
            record_from_card(authority),
            revoked_record=record_from_card(rewritten),
        )
    current = await store.read_current_authority(
        subject_hash=subject_hash, access_id=authority.access_id
    )
    assert current is not None and current[1] == authority

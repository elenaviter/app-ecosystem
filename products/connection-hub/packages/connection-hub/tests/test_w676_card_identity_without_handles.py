"""An agent Card whose resident bearer cannot be read never blocks the owner's other Cards.

Live (dev-main, 2026-10-09, Card transactions on): every OAuth refresh of one person's agents was withheld as
"delegated card conflict reason=delegated_cards_unavailable" (503). ``record_oauth_grant`` resolves the Card
identity by listing every Card of the owner (``list_all_current``), and that listing also read each Card's
credential handles; one agent Card whose resident bearer was unreadable failed the whole listing. The
listings use authorities only, so they no longer read handles; a Card that needs its bearer still reads it
when it is used.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from test_credentialless_card_persistence import OWNER, SUBJECT_HASH, _Handles, _Store, _agent, _persistence


class _ListingStore(_Store):
    def __init__(self, authorities, *, unreadable=()):
        super().__init__(authorities)
        self._unreadable = set(unreadable)

    async def list_card_ids(self, *, subject_hash: str):
        return sorted(self._authorities)

    async def read_current_authority(self, *, subject_hash: str, access_id: str):
        if access_id in self._unreadable:
            raise OSError("synthetic unreadable revision")
        return await super().read_current_authority(subject_hash=subject_hash, access_id=access_id)


def _oauth_card():
    return dataclasses.replace(_agent(), access_id="oauth-card-1", client_id="dcr-client-1",
                               card_kind=CARD_KIND_AUTOMATION, source="oauth",
                               delegate_subject=f"integration:dcr-client-1:{OWNER}", label="Agent")


def _owner_cards(*, unreadable_revision=()):
    agent, oauth = _agent(), _oauth_card()
    authorities = {agent.access_id: agent, oauth.access_id: oauth}
    handles = _Handles({})  # no handle readable at all: the agent's resident bearer is missing
    persistence = _persistence(authorities, handles)
    persistence._store = _ListingStore(authorities, unreadable=unreadable_revision)  # type: ignore[attr-defined]
    return persistence, handles, agent, oauth


@pytest.mark.asyncio
async def test_listing_the_owners_cards_reads_no_credential_handles():
    persistence, handles, agent, oauth = _owner_cards()
    listed = await persistence.list_all_current(subject_hash=SUBJECT_HASH)
    assert {authority.access_id for authority in listed} == {agent.access_id, oauth.access_id}
    assert handles.read_ids == [] and handles.read_current_ids == []
    assert {a.access_id for a in await persistence.list_current(subject_hash=SUBJECT_HASH)} == {
        agent.access_id, oauth.access_id}


@pytest.mark.asyncio
async def test_another_owners_card_is_never_listed():
    persistence, _, agent, oauth = _owner_cards()
    assert await persistence.list_all_current(subject_hash="another-subject-hash") == []


@pytest.mark.asyncio
async def test_an_unreadable_revision_still_refuses_the_listing():
    persistence, _, agent, _ = _owner_cards(unreadable_revision=("agent-card-1",))
    with pytest.raises(CardUnavailable, match="durable_card_unreadable"):
        await persistence.list_all_current(subject_hash=SUBJECT_HASH)


@pytest.mark.asyncio
async def test_the_oauth_card_identity_resolves_although_an_agent_bearer_is_unreadable():
    persistence, handles, agent, oauth = _owner_cards()
    service = AutomationAccessService(redis=object(), tenant="tenant-a", project="project-a",
                                      config=None, grant_store=object(), card_persistence=persistence)
    records = await service._list_all_owner_records(OWNER)
    assert {record.access_id for record in records} == {agent.access_id, oauth.access_id}
    identity = await service.resolve_card_identity(grantor_subject=OWNER, client_id=oauth.client_id,
                                                   card_kind=CARD_KIND_AUTOMATION, entry_resource="")
    assert identity.get("ok") is True and identity.get("access_id") == oauth.access_id, identity


@pytest.mark.asyncio
async def test_a_refused_identity_names_its_reason_in_the_log(caplog):
    persistence, _, agent, oauth = _owner_cards(unreadable_revision=("agent-card-1",))
    service = AutomationAccessService(redis=object(), tenant="tenant-a", project="project-a",
                                      config=None, grant_store=object(), card_persistence=persistence)
    caplog.set_level(logging.WARNING)
    from connection_hub.delegated_credentials.cards.service import CardConflict
    with pytest.raises(CardConflict, match="delegated_cards_unavailable"):
        await service.record_oauth_grant(grantor_subject=OWNER, client_id=oauth.client_id, client_label="Agent",
                                         scopes=["messages:read"], resource="", access_token="at-x",
                                         refresh_token="rt-x", card_kind=CARD_KIND_AUTOMATION)
    assert "card identity refused" in caplog.text and "reason=durable_card_unreadable" in caplog.text
    assert "at-x" not in caplog.text and "rt-x" not in caplog.text

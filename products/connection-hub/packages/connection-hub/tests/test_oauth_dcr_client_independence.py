# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Independent OAuth Cards for native clients registered through DCR."""

from __future__ import annotations

import itertools
import time
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.automation_access import (
    ACCESS_SOURCE_OAUTH,
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_REVOKED,
    CardAuthority,
    CardCredentialHandles,
    authority_is_usable,
)
from connection_hub.delegated_credentials.cards.service import CardConflict, replace_state
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)


GRANTOR = "user-1"
RESOURCE = "https://hub.example.test/mcp"
CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {
                    "grant": "fixture:use",
                    "label": "Use fixture",
                    "delegable_roles": ["kdcube:role:registered"],
                }
            ],
            "resources": [
                {
                    "resource": RESOURCE,
                    "label": "Fixture",
                    "identity_scope": "grantor",
                    "tools": {
                        "search": {
                            "label": "Search",
                            "grants": ["fixture:use"],
                        }
                    },
                }
            ],
        }
    }
}


class _Catalog:
    def __init__(self) -> None:
        self.document = CatalogDocument.build(
            CONNECTIONS,
            created_at=datetime.fromtimestamp(1_780_000_000, tz=timezone.utc),
        )

    async def resolve_active(self):
        return self.document

    async def resolve_version(self, version):
        return self.document if version == self.document.version else None


class _Persistence:
    def __init__(self) -> None:
        self.cards: dict[str, tuple[CardAuthority, CardCredentialHandles]] = {}

    @staticmethod
    def _owned(authority: CardAuthority, subject_hash: str) -> bool:
        return subject_hash_for(authority.grantor_subject) == subject_hash

    async def load(self, access_id, *, subject_hash):
        entry = self.cards.get(access_id)
        if entry is None or not self._owned(entry[0], subject_hash):
            return None
        if not authority_is_usable(entry[0], int(time.time())):
            return None
        return entry

    async def current_revision(self, access_id, *, subject_hash):
        entry = self.cards.get(access_id)
        if entry is None or not self._owned(entry[0], subject_hash):
            return 0
        return entry[0].card_revision

    async def persist(self, authority, handles, *, subject_hash, expected_revision):
        current = await self.current_revision(
            authority.access_id,
            subject_hash=subject_hash,
        )
        if current != int(expected_revision):
            raise CardConflict("card_revision_moved", current_revision=current)
        self.cards[authority.access_id] = (authority, handles)

    async def forget(self, authority, *, subject_hash):
        if not self._owned(authority, subject_hash):
            return
        self.cards[authority.access_id] = (
            replace_state(authority, CARD_STATE_REVOKED),
            CardCredentialHandles(access_id=authority.access_id),
        )

    async def list_active(self, *, subject_hash, now=None):
        moment = int(now if now is not None else time.time())
        return [
            authority
            for authority, _handles in self.cards.values()
            if self._owned(authority, subject_hash)
            and authority_is_usable(authority, moment)
        ]


class _GrantStore:
    refresh_ttl = 86400

    def __init__(self, client_records) -> None:
        self.client_records = dict(client_records)
        self.client_record_reads: list[str] = []
        self.revoked_access: list[str] = []
        self.revoked_refresh: list[str] = []

    async def get_client_record(self, client_id):
        self.client_record_reads.append(client_id)
        return self.client_records.get(client_id)

    async def revoke_access_grant(self, token):
        self.revoked_access.append(token)
        return True

    async def revoke_refresh_token(self, token):
        self.revoked_refresh.append(token)
        return True


class _Redis:
    async def zremrangebyscore(self, *_args):
        return 0

    async def zrange(self, *_args):
        return []


def _service(store: _GrantStore, persistence: _Persistence) -> AutomationAccessService:
    return AutomationAccessService(
        redis=_Redis(),
        tenant="tenant-a",
        project="project-a",
        config=oauth_delegated_config_from_connections(CONNECTIONS),
        grant_store=store,
        catalog_resolver=_Catalog(),
        card_persistence=persistence,
    )


@pytest.mark.parametrize("loopback_host", ["127.0.0.1", "localhost", "[::1]"])
@pytest.mark.asyncio
async def test_dcr_loopback_clients_keep_independent_cards(loopback_host):
    clients = ("dcr-openclaw", "dcr-hermes", "dcr-third")
    store = _GrantStore(
        {
            client_id: {
                "redirect_uris": [
                    f"http://{loopback_host}:{port}/oauth/callback"
                ]
            }
            for client_id, port in zip(clients, (41001, 52002, 63003), strict=True)
        }
    )
    persistence = _Persistence()
    service = _service(store, persistence)

    records = []
    for index, client_id in enumerate(clients, start=1):
        record = await service.record_oauth_grant(
            grantor_subject=GRANTOR,
            client_id=client_id,
            client_label=client_id.removeprefix("dcr-").title(),
            scopes=["fixture:use"],
            operations=["search"],
            resource=RESOURCE,
            access_token=f"access-{index}",
            refresh_token=f"refresh-{index}",
            account_scope={
                "fixture": {f"account-{index}": [f"claim-{index}"]}
            },
        )
        assert record is not None
        assert record.source == ACCESS_SOURCE_OAUTH
        records.append(record)

    active = await service._list_active_records(GRANTOR)
    assert {record.access_id for record in active} == {
        record.access_id for record in records
    }
    assert len({record.access_id for record in records}) == 3
    assert [record.account_scope for record in records] == [
        {"fixture": {"account-1": ("claim-1",)}},
        {"fixture": {"account-2": ("claim-2",)}},
        {"fixture": {"account-3": ("claim-3",)}},
    ]
    assert store.client_record_reads == []
    assert store.revoked_access == []
    assert store.revoked_refresh == []

    assert await service.oauth_seed_account_scope(
        grantor_subject=GRANTOR,
        client_id=clients[0],
        resource=RESOURCE,
    ) == {"fixture": {"account-1": ["claim-1"]}}
    assert await service.oauth_seed_account_scope(
        grantor_subject=GRANTOR,
        client_id="dcr-new-client",
        resource=RESOURCE,
    ) == {}
    assert await service.oauth_seed_named_service_operations(
        grantor_subject=GRANTOR,
        client_id="dcr-new-client",
        resource=RESOURCE,
    ) == {}
    assert store.client_record_reads == []

    first = records[0]
    refreshed = await service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id=first.client_id,
        client_label="OpenClaw",
        scopes=["fixture:use"],
        resource=RESOURCE,
        access_token="access-1-rotated",
        refresh_token="refresh-1-rotated",
    )
    assert refreshed is not None
    assert refreshed.access_id == first.access_id
    assert refreshed.card_revision == first.card_revision + 1
    assert refreshed.account_scope == first.account_scope
    assert len(await service._list_active_records(GRANTOR)) == 3

    user = {"user_id": GRANTOR}
    revoked_second = await service.revoke_access(
        user,
        access_id=records[1].access_id,
    )
    assert revoked_second["ok"] is True
    assert {
        record.access_id for record in await service._list_active_records(GRANTOR)
    } == {first.access_id, records[2].access_id}
    assert store.revoked_access == ["access-2"]
    assert store.revoked_refresh == ["refresh-2"]

    revoked_first = await service.revoke_access(user, access_id=first.access_id)
    assert revoked_first["ok"] is True
    assert {
        record.access_id for record in await service._list_active_records(GRANTOR)
    } == {records[2].access_id}
    assert store.revoked_access == ["access-2", "access-1-rotated"]
    assert store.revoked_refresh == ["refresh-2", "refresh-1-rotated"]

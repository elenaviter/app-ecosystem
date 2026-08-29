# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

import pytest

from prokura.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    NamedServiceSelection,
)
from prokura.delegated_credentials.cards.service import (
    CARD_LOCK_WAIT_SECONDS,
    CardConflict,
    CardMutationLockTimeout,
    DelegatedCardService,
)
from prokura.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

SUBJECT_HASH = hashlib.sha256(b"platform-user-1").hexdigest()
ACCESS_ID = "aut_abc123"
NOW = 1_780_000_000


class _Cache:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def claim_transition(self, *args, **kwargs) -> bool:
        return True

    async def commit_projection(self, *args, **kwargs) -> bool:
        return True

    async def index_add(self, *, access_id: str, **kwargs) -> None:
        self.indexed.append(access_id)

    async def index_remove(self, *, access_id: str, **kwargs) -> None:
        if access_id in self.indexed:
            self.indexed.remove(access_id)

    async def finalize_removal(self, *args, **kwargs) -> None:
        return None

    async def read(self, *args, **kwargs):
        return None


def _authority() -> CardAuthority:
    return CardAuthority(
        access_id=ACCESS_ID,
        client_id="automation:abc",
        grantor_subject="platform-user-1",
        delegate_subject="integration:automation:abc",
        source="manual",
        label="CI bot",
        card_revision=1,
        catalog_version="catalog-v1",
        state=CARD_STATE_ACTIVE,
        resource_grants={"https://example.test/mcp": ("messages:read",)},
        named_service_operations=NamedServiceSelection.none(),
        created_at=NOW,
        expires_at=NOW + 3600,
    )


@pytest.mark.asyncio
async def test_service_uses_host_lock_and_commits_authority(tmp_path):
    calls = []

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        calls.append(kwargs)
        yield {"owner": "test"}

    store = BundleStorageDelegatedCardStore(tmp_path)
    cache = _Cache()
    service = DelegatedCardService(
        store=store,
        cache=cache,
        mutation_lock=mutation_lock,
    )

    pointer = await service.commit(
        _authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW
    )

    assert pointer.card_revision == 1
    assert cache.indexed == [ACCESS_ID]
    assert calls == [
        {
            "lock_path": store.card_path(
                subject_hash=SUBJECT_HASH, access_id=ACCESS_ID
            )
            / ".mutation.lock",
            "resource_id": f"delegated-card:{ACCESS_ID}",
            "operation": "delegated-card-mutation",
            "wait_seconds": CARD_LOCK_WAIT_SECONDS,
        }
    ]


@pytest.mark.asyncio
async def test_lock_timeout_is_a_card_conflict(tmp_path):
    @asynccontextmanager
    async def mutation_lock(**kwargs):
        raise CardMutationLockTimeout("busy")
        yield

    service = DelegatedCardService(
        store=BundleStorageDelegatedCardStore(tmp_path),
        cache=_Cache(),
        mutation_lock=mutation_lock,
    )

    with pytest.raises(CardConflict) as exc:
        await service.commit(
            _authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW
        )

    assert exc.value.reason == "card_mutation_lock_timeout"

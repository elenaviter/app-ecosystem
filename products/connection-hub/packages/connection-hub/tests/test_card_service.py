# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CARD_AUTHORITY_SCHEMA,
    CARD_AUTHORITY_SCHEMA_V1,
    CARD_STATE_ACTIVE,
    CardAuthority,
    CardCurrentPointer,
    NamedServiceSelection,
    card_authority_payload_hash,
    card_revision_name,
)
from connection_hub.delegated_credentials.cards.service import (
    CARD_LOCK_WAIT_SECONDS,
    CardConflict,
    CardMutationLockTimeout,
    DelegatedCardService,
)
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.durable_io import write_json_atomic

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
        resource_operations={"https://example.test/mcp": ("messages.search",)},
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


def test_v1_card_keeps_flat_authority_and_next_write_is_resource_qualified() -> None:
    legacy = _authority().to_dict()
    legacy["schema"] = CARD_AUTHORITY_SCHEMA_V1
    legacy["resource_grants"] = {
        "https://a.example/mcp": ["messages:read"],
        "https://b.example/mcp": ["messages:read"],
    }
    legacy["operations"] = ["search"]
    legacy.pop("resource_operations")

    migrated = CardAuthority.from_mapping(legacy)

    assert migrated.resource_operations == {
        "https://a.example/mcp": ("search",),
        "https://b.example/mcp": ("search",),
    }
    rewritten = migrated.to_dict()
    assert rewritten["schema"] == CARD_AUTHORITY_SCHEMA
    assert rewritten["resource_operations"] == {
        "https://a.example/mcp": ["search"],
        "https://b.example/mcp": ["search"],
    }


@pytest.mark.asyncio
async def test_v1_current_pointer_hash_is_verified_before_projection(tmp_path) -> None:
    store = BundleStorageDelegatedCardStore(tmp_path)
    legacy = _authority().to_dict()
    legacy["schema"] = CARD_AUTHORITY_SCHEMA_V1
    legacy["resource_grants"] = {
        "https://a.example/mcp": ["messages:read"],
        "https://b.example/mcp": ["messages:read"],
    }
    legacy["operations"] = ["search"]
    legacy.pop("resource_operations")
    content_hash = card_authority_payload_hash(legacy)
    updated_at = datetime.fromtimestamp(NOW, tz=timezone.utc)
    revision_name = card_revision_name(
        card_revision=1,
        content_hash=content_hash,
        updated_at=updated_at,
    )
    migrated = CardAuthority.from_mapping(legacy)
    pointer = CardCurrentPointer.for_revision(
        migrated,
        revision_name=revision_name,
        content_hash=content_hash,
        updated_at=updated_at,
    )
    await write_json_atomic(
        store.revision_path(
            subject_hash=SUBJECT_HASH,
            access_id=ACCESS_ID,
            revision_name=revision_name,
        ),
        legacy,
    )
    await write_json_atomic(
        store.current_path(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID),
        pointer.to_dict(),
    )

    loaded = await store.read_current_authority(
        subject_hash=SUBJECT_HASH,
        access_id=ACCESS_ID,
    )

    assert loaded is not None
    assert loaded[1].resource_operations == {
        "https://a.example/mcp": ("search",),
        "https://b.example/mcp": ("search",),
    }

from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import Any

import pytest

from connection_hub.delegated_credentials.cards.credential_handles import (
    CardCredentialHandleUnavailable,
    PostgresCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_REVOKED,
    CardHandleMetadata,
)
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AGENT,
    CARD_KIND_AUTOMATION,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    CardCredentialHandles,
)


def _authority(*, card_kind: str, revision: int = 3) -> CardAuthority:
    now = int(time.time())
    return CardAuthority(
        access_id="agent-card-1" if card_kind == CARD_KIND_AGENT else "aut_card_1",
        client_id="client-1",
        grantor_subject="user-1",
        delegate_subject="agent-1",
        source="agent" if card_kind == CARD_KIND_AGENT else "oauth",
        card_kind=card_kind,
        card_revision=revision,
        created_at=now - 60,
        expires_at=now + 3600,
    )


class _MetadataStore:
    def __init__(self) -> None:
        self.current: CardHandleMetadata | None = None

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        if self.current is None or self.current.access_id != access_id:
            return None
        return self.current

    async def read_active(
        self,
        access_id: str,
        *,
        now: int | None = None,
    ) -> CardHandleMetadata | None:
        current = await self.read_current(access_id)
        if current is None or not current.serves_at(now or int(time.time())):
            return None
        return current

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata:
        held_revision = self.current.revision if self.current is not None else 0
        if held_revision != expected_revision:
            raise RuntimeError("stale metadata revision")
        self.current = dataclasses.replace(
            metadata,
            revision=expected_revision + 1,
        )
        return self.current


class _ResidentSecrets:
    def __init__(self, metadata: _MetadataStore) -> None:
        self._metadata = metadata
        self.bearer = ""
        self.installs: list[dict[str, Any]] = []
        self.retirements: list[tuple[str, int, str]] = []

    async def install(self, **kwargs: Any) -> None:
        self.installs.append(dict(kwargs))
        self.bearer = str(kwargs["bearer"])
        self._metadata.current = CardHandleMetadata(
            access_id=str(kwargs["access_id"]),
            card_revision=int(kwargs["card_revision"]),
            expires_at=int(kwargs["expires_at"]),
            resident_access_secret_ref="a" * 32,
            resident_access_sha256=hashlib.sha256(
                str(kwargs["bearer"]).encode("utf-8")
            ).hexdigest(),
            session_id=str(kwargs["session_id"]),
            revision=int(kwargs["expected_revision"]) + 1,
        ).validated()

    async def resolve(self, access_id: str) -> str:
        assert self._metadata.current is not None
        assert self._metadata.current.access_id == access_id
        return self.bearer

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> None:
        self.retirements.append((access_id, expected_revision, state))
        assert self._metadata.current is not None
        self._metadata.current = dataclasses.replace(
            self._metadata.current,
            state=state,
            revision=expected_revision + 1,
        )


def _store() -> tuple[
    PostgresCardCredentialHandleStore,
    _MetadataStore,
    _ResidentSecrets,
]:
    metadata = _MetadataStore()
    resident = _ResidentSecrets(metadata)
    return (
        PostgresCardCredentialHandleStore(
            metadata_store=metadata,
            resident_secrets=resident,
        ),
        metadata,
        resident,
    )


@pytest.mark.asyncio
async def test_resident_bearer_uses_host_custody_and_exact_card_binding() -> None:
    store, metadata, resident = _store()
    authority = _authority(card_kind=CARD_KIND_AGENT)

    await store.write(
        authority,
        CardCredentialHandles(
            access_id=authority.access_id,
            access_token="resident-bearer",
            session_id="session-1",
        ),
    )
    loaded = await store.read(authority)

    assert loaded == CardCredentialHandles(
        access_id=authority.access_id,
        access_token="resident-bearer",
        session_id="session-1",
    )
    assert resident.installs[0]["expected_revision"] == 0
    assert metadata.current is not None
    assert metadata.current.resident_access_secret_ref == "a" * 32
    assert metadata.current.resident_access_sha256 == hashlib.sha256(
        b"resident-bearer"
    ).hexdigest()

    moved = dataclasses.replace(authority, card_revision=authority.card_revision + 1)
    with pytest.raises(CardCredentialHandleUnavailable, match="revision_mismatch"):
        await store.read(moved)


@pytest.mark.asyncio
async def test_oauth_bearers_are_not_copied_into_card_handle_metadata() -> None:
    store, metadata, resident = _store()
    authority = _authority(card_kind=CARD_KIND_AUTOMATION)

    await store.write(
        authority,
        CardCredentialHandles(
            access_id=authority.access_id,
            access_token="raw-access-bearer",
            refresh_token="raw-refresh-bearer",
            session_id="session-2",
        ),
    )
    loaded = await store.read(authority)

    assert loaded == CardCredentialHandles(
        access_id=authority.access_id,
        session_id="session-2",
    )
    assert metadata.current is not None
    assert metadata.current.resident_access_secret_ref == ""
    assert metadata.current.resident_access_sha256 == ""
    assert resident.installs == []


@pytest.mark.asyncio
async def test_migration_rerun_proves_exact_resident_secret_without_rewriting() -> None:
    store, _metadata, resident = _store()
    authority = _authority(card_kind=CARD_KIND_AGENT)
    handles = CardCredentialHandles(
        access_id=authority.access_id,
        access_token="resident-bearer",
        session_id="session-1",
    )

    assert await store.import_current(authority, handles)
    assert not await store.import_current(authority, handles)
    assert len(resident.installs) == 1

    with pytest.raises(CardCredentialHandleUnavailable, match="target_conflict"):
        await store.import_current(
            authority,
            dataclasses.replace(handles, access_token="different-bearer"),
        )
    assert len(resident.installs) == 1


@pytest.mark.asyncio
async def test_removal_commits_terminal_metadata_before_secret_cleanup() -> None:
    store, metadata, resident = _store()
    authority = _authority(card_kind=CARD_KIND_AGENT)
    await store.write(
        authority,
        CardCredentialHandles(
            access_id=authority.access_id,
            access_token="resident-bearer",
        ),
    )

    await store.remove(authority)

    assert metadata.current is not None
    assert metadata.current.state == HANDLE_STATE_REVOKED
    assert resident.retirements == [
        (authority.access_id, 1, HANDLE_STATE_REVOKED)
    ]
    assert HANDLE_STATE_ACTIVE != metadata.current.state

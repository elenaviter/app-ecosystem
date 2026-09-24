# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    CardCredentialHandles,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from connection_hub.delegated_credentials.cards.service import CardConflict
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import (
    new_credentialless_card,
)

OWNER = "platform-user-1"
SUBJECT_HASH = subject_hash_for(OWNER)
RESOURCE = "https://example.test/mcp/messages"


class _Resolver:
    def __init__(self, authorities: dict[str, CardAuthority]) -> None:
        self._authorities = authorities

    async def resolve(self, *, subject_hash: str, access_id: str):
        authority = self._authorities.get(access_id)
        if authority is None:
            return None
        if subject_hash_for(authority.grantor_subject) != subject_hash:
            return None
        if authority.state != CARD_STATE_ACTIVE:
            return None
        return authority


class _Cards:
    def __init__(self, authorities: dict[str, CardAuthority]) -> None:
        self._authorities = authorities

    async def commit(
        self,
        authority: CardAuthority,
        *,
        subject_hash: str,
        expected_revision: int,
        now: int,
    ) -> object:
        del now
        if subject_hash_for(authority.grantor_subject) != subject_hash:
            raise AssertionError("wrong owner")
        current = self._authorities.get(authority.access_id)
        current_revision = current.card_revision if current is not None else 0
        if current_revision != expected_revision:
            raise CardConflict(
                "card_revision_moved",
                current_revision=current_revision,
            )
        self._authorities[authority.access_id] = authority
        return object()


class _Store:
    def __init__(self, authorities: dict[str, CardAuthority]) -> None:
        self._authorities = authorities

    async def read_current_authority(self, *, subject_hash: str, access_id: str):
        authority = self._authorities.get(access_id)
        if authority is None:
            return None
        if subject_hash_for(authority.grantor_subject) != subject_hash:
            return None
        return object(), authority


class _Handles:
    def __init__(
        self,
        handles: dict[str, CardCredentialHandles] | None = None,
    ) -> None:
        self._handles = dict(handles or {})
        self.read_ids: list[str] = []
        self.read_current_ids: list[str] = []
        self.written_ids: list[str] = []
        self.removed_ids: list[str] = []

    async def read(self, authority: CardAuthority) -> CardCredentialHandles:
        self.read_ids.append(authority.access_id)
        try:
            return self._handles[authority.access_id]
        except KeyError as exc:
            raise RuntimeError("card_handle_metadata_missing") from exc

    async def read_current(
        self,
        authority: CardAuthority,
    ) -> CardCredentialHandles:
        self.read_current_ids.append(authority.access_id)
        try:
            return self._handles[authority.access_id]
        except KeyError as exc:
            raise RuntimeError("card_handle_metadata_missing") from exc

    async def write(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> None:
        self.written_ids.append(authority.access_id)
        self._handles[authority.access_id] = handles

    async def remove(self, authority: CardAuthority) -> None:
        self.removed_ids.append(authority.access_id)
        self._handles.pop(authority.access_id, None)


def _agent() -> CardAuthority:
    now = int(time.time())
    return CardAuthority(
        access_id="agent-card-1",
        client_id="kdcube-agent:workspace:main",
        grantor_subject=OWNER,
        delegate_subject=f"integration:agent:{OWNER}",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        label="Workspace main",
        card_revision=1,
        catalog_version="catalog-v1",
        state=CARD_STATE_ACTIVE,
        resource_grants={RESOURCE: ("messages:read",)},
        resource_operations={RESOURCE: ("messages.search",)},
        named_service_operations=NamedServiceSelection.none(),
        named_services={},
        account_scope={},
        created_at=now - 60,
        expires_at=now + 3600,
    )


def _control(agent: CardAuthority) -> CardAuthority:
    return new_credentialless_card(
        initial_selection=agent,
        grantor_subject=OWNER,
        catalog_version=agent.catalog_version,
        control_id="control-project-1",
        issuer_ref="work:project:demo",
        issuer_kind="application",
        issuer_label="Demo project",
        manage_url="/problem-board/projects/demo/control",
        now=int(time.time()),
    )


def _persistence(
    authorities: dict[str, CardAuthority],
    handles: _Handles,
) -> DurableCardPersistence:
    persistence = DurableCardPersistence(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        card_store=object(),
        mutation_lock=object(),  # type: ignore[arg-type]
        credential_handles=handles,
        authority_backend="postgresql",
    )
    persistence._resolver = _Resolver(authorities)  # type: ignore[attr-defined]
    persistence._cards = _Cards(authorities)  # type: ignore[attr-defined]
    persistence._store = _Store(authorities)  # type: ignore[attr-defined]
    return persistence


@pytest.mark.asyncio
async def test_migrated_credentialless_card_loads_and_saves_without_handle_metadata() -> None:
    control = _control(_agent())
    authorities = {control.access_id: control}
    handles = _Handles()
    persistence = _persistence(authorities, handles)

    empty = CardCredentialHandles(access_id=control.access_id)
    assert await persistence.load(
        control.access_id,
        subject_hash=SUBJECT_HASH,
    ) == (control, empty)
    assert await persistence.load_current(
        control.access_id,
        subject_hash=SUBJECT_HASH,
    ) == (control, empty)

    updated = dataclasses.replace(
        control,
        label="Renamed project control",
        card_revision=control.card_revision + 1,
    )
    await persistence.persist(
        updated,
        empty,
        subject_hash=SUBJECT_HASH,
        expected_revision=control.card_revision,
    )

    assert await persistence.load(
        control.access_id,
        subject_hash=SUBJECT_HASH,
    ) == (updated, empty)
    assert handles.read_ids == []
    assert handles.read_current_ids == []
    assert handles.written_ids == []
    assert handles.removed_ids == [control.access_id]


@pytest.mark.asyncio
async def test_agent_can_attach_a_credentialless_control_without_control_handle_metadata() -> None:
    agent = _agent()
    control = _control(agent)
    authorities = {
        agent.access_id: agent,
        control.access_id: control,
    }
    handles = _Handles(
        {
            agent.access_id: CardCredentialHandles(
                access_id=agent.access_id,
                access_token="resident-token",
                session_id="session-1",
            )
        }
    )
    persistence = _persistence(authorities, handles)
    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=None,  # type: ignore[arg-type]
        grant_store=object(),
        card_persistence=persistence,
    )
    service.notify_change = AsyncMock()

    result = await service.attach_control_card(
        {"user_id": OWNER},
        access_id=agent.access_id,
        control_id=control.access_id,
        expected_card_revision=agent.card_revision,
    )

    assert result["ok"] is True, result
    assert result["attached"] is True
    assert result["control_card"]["state"] == "active"
    assert authorities[agent.access_id].control_card is not None
    assert authorities[agent.access_id].control_card.control_id == control.access_id
    assert control.access_id not in handles.read_ids
    assert control.access_id not in handles.read_current_ids
    assert handles.written_ids == [agent.access_id]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    record_from_card,
)
from connection_hub.delegated_credentials.cache_io import (
    decode_cache_value,
    encode_cache_value,
)
from connection_hub.delegated_credentials.cards.cache import (
    CARD_CACHE_KIND_CARD,
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_AUTHORITY_SCHEMA,
    CARD_AUTHORITY_SCHEMA_V4,
    CARD_STATE_ACTIVE,
    CardAuthority,
    CardCredentialHandles,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.service import (
    CardConflict,
    replace_state,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.cache import (
    CONTROL_CACHE_KIND_CARD,
    CONTROL_CACHE_KIND_UPDATING,
    ControlCardRuntimeCache,
)
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.model import (
    ProjectControlCardAuthority,
    control_card_is_subset,
)
from connection_hub.delegated_credentials.live_grant import (
    LiveGrantCardError,
    resolve_live_grant_card,
)


RESOURCE = "https://example.test/mcp/named-services"
OWNER = "platform-user-1"
CONTROL_ID = "project-control-abc123"
PROJECT_REF = "work:project:demo"


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)


class _Persistence:
    def __init__(self, authority: CardAuthority) -> None:
        self.authority = authority
        self.handles = CardCredentialHandles(access_id=authority.access_id)
        self.persist_calls = 0

    def _owned(self, subject_hash: str) -> bool:
        return subject_hash_for(self.authority.grantor_subject) == subject_hash

    async def load(self, access_id: str, *, subject_hash: str):
        if (
            access_id != self.authority.access_id
            or not self._owned(subject_hash)
            or self.authority.state != CARD_STATE_ACTIVE
            or self.authority.expires_at <= int(time.time())
        ):
            return None
        return self.authority, self.handles

    async def load_current(self, access_id: str, *, subject_hash: str):
        if access_id != self.authority.access_id or not self._owned(subject_hash):
            return None
        return self.authority, self.handles

    async def persist(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
        *,
        subject_hash: str,
        expected_revision: int,
    ) -> None:
        if not self._owned(subject_hash):
            raise AssertionError("wrong owner")
        if expected_revision != self.authority.card_revision:
            raise CardConflict(
                "card_revision_moved",
                current_revision=self.authority.card_revision,
            )
        self.authority = authority
        self.handles = handles
        self.persist_calls += 1


def _named_services() -> dict:
    return {
        "namespaces": {
            "slack": {
                "tools": {
                    "object_action": {
                        "operations": {
                            "object.action.post_message": {
                                "grants": ["slack:post"],
                            },
                            "object.action.upload_file": {
                                "grants": ["slack:files:write"],
                            },
                        }
                    }
                }
            }
        }
    }


def _card(*, binding: ControlCardBinding | None = None) -> CardAuthority:
    return CardAuthority(
        access_id="agent-card-1",
        client_id="kdcube-agent:workspace:main",
        grantor_subject=OWNER,
        delegate_subject=f"integration:agent:{OWNER}",
        source="agent",
        label="Workspace main",
        card_revision=3,
        catalog_version="catalog-v1",
        state=CARD_STATE_ACTIVE,
        resource_grants={RESOURCE: ("named_services:use",)},
        resource_operations={
            RESOURCE: (
                "object.action.post_message",
                "object.action.upload_file",
            )
        },
        named_service_operations=NamedServiceSelection.exact(
            {
                RESOURCE: {
                    "slack": (
                        "object.action.post_message",
                        "object.action.upload_file",
                    )
                }
            }
        ),
        named_services=_named_services(),
        account_scope={
            "slack": {
                "workspace-1": ("slack:post", "slack:files:write"),
            }
        },
        created_at=int(time.time()) - 60,
        expires_at=int(time.time()) + 3600,
        control_card=binding,
    )


def _control(*, operations: tuple[str, ...] = ("object.action.post_message",)):
    basis = ProjectControlCardAuthority.from_card(
        _card(),
        control_id=CONTROL_ID,
        issuer_ref=PROJECT_REF,
        issuer_label="Demo project",
        manage_url="/problem-board/projects/demo/control",
        now=int(time.time()),
    )
    payload = basis.to_dict()
    payload["revision"] = 2
    payload["resource_operations"] = {RESOURCE: list(operations)}
    payload["named_service_operations"] = {
        RESOURCE: {"slack": list(operations)}
    }
    payload["account_scope"] = {
        "slack": {"workspace-1": ["slack:post"]}
    }
    return ProjectControlCardAuthority.from_mapping(payload)


def _bound_card(control: ProjectControlCardAuthority) -> CardAuthority:
    return dataclasses.replace(
        _card(),
        control_card=ControlCardBinding(
            control_id=control.control_id,
            issuer_ref=control.issuer_ref,
            issuer_label=control.issuer_label,
            manage_url=control.manage_url,
            control_revision=control.revision,
        ),
    )


def _put_card(redis: _Redis, card: CardAuthority) -> None:
    key = DelegatedCardRuntimeCache(
        redis,
        tenant="tenant",
        project="project",
    ).card_key(card.access_id)
    redis.values[key] = encode_cache_value(
        {
            "kind": CARD_CACHE_KIND_CARD,
            "card_revision": card.card_revision,
            "authority": card.to_dict(),
        }
    )


def _put_control(redis: _Redis, control: ProjectControlCardAuthority) -> None:
    key = ControlCardRuntimeCache(
        redis,
        tenant="tenant",
        project="project",
    ).key(control.control_id)
    redis.values[key] = encode_cache_value(
        {
            "kind": CONTROL_CACHE_KIND_CARD,
            "revision": control.revision,
            "authority": control.to_dict(),
        }
    )


def test_v4_card_remains_readable_and_v5_round_trips_one_control_binding() -> None:
    legacy = _card().to_dict()
    legacy["schema"] = CARD_AUTHORITY_SCHEMA_V4
    legacy.pop("control_card", None)

    assert CardAuthority.from_mapping(legacy).control_card is None

    control = _control()
    stored = _bound_card(control).to_dict()
    restored = CardAuthority.from_mapping(stored)
    assert stored["schema"] == CARD_AUTHORITY_SCHEMA
    assert restored.control_card is not None
    assert restored.control_card.control_id == CONTROL_ID


def test_effective_authority_intersects_tools_and_account_claims_independently() -> None:
    control = _control()
    effective = effective_card_authority(_bound_card(control), control)

    assert effective.resource_operations == {
        RESOURCE: ("object.action.post_message",)
    }
    assert effective.named_service_operations.operations == {
        RESOURCE: {"slack": ("object.action.post_message",)}
    }
    assert effective.account_scope == {
        "slack": {"workspace-1": ("slack:post",)}
    }
    assert "object.action.upload_file" not in str(effective.named_services)


def test_effective_authority_keeps_an_explicitly_claimless_operation() -> None:
    operation = "object.schema"
    card = dataclasses.replace(
        _card(),
        resource_grants={RESOURCE: ()},
        resource_operations={RESOURCE: (operation,)},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"slack": (operation,)}}
        ),
        named_services={
            "namespaces": {
                "slack": {
                    "tools": {
                        "object_schema": {
                            "operations": {operation: {"grants": []}},
                        }
                    }
                }
            }
        },
        account_scope={},
    )
    control = ProjectControlCardAuthority.from_card(
        card,
        control_id=CONTROL_ID,
        issuer_ref=PROJECT_REF,
        now=int(time.time()),
    )
    bound = dataclasses.replace(
        card,
        control_card=ControlCardBinding(
            control_id=control.control_id,
            issuer_ref=control.issuer_ref,
            control_revision=control.revision,
        ),
    )

    effective = effective_card_authority(bound, control)

    assert effective.resource_grants == {RESOURCE: ()}
    assert effective.resource_operations == {RESOURCE: (operation,)}
    assert effective.named_service_operations.operations == {
        RESOURCE: {"slack": (operation,)}
    }


def test_project_control_cannot_add_authority_missing_from_its_basis() -> None:
    maximum = _control()
    payload = maximum.to_dict()
    payload["account_scope"]["slack"]["workspace-1"].append("slack:admin")
    candidate = ProjectControlCardAuthority.from_mapping(payload)

    assert not control_card_is_subset(candidate, maximum)


def test_project_control_records_the_exact_card_and_catalog_basis() -> None:
    control = ProjectControlCardAuthority.from_card(
        _card(),
        control_id=CONTROL_ID,
        issuer_ref=PROJECT_REF,
        now=int(time.time()),
    )

    assert control.basis_access_id == "agent-card-1"
    assert control.basis_card_revision == 3
    assert control.basis_catalog_version == "catalog-v1"


@pytest.mark.asyncio
async def test_effective_view_reports_both_card_and_control_catalog_evidence() -> None:
    redis = _Redis()
    control = _control()
    card = _bound_card(control)
    _put_control(redis, control)
    service = AutomationAccessService(
        redis=redis,
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=_Persistence(card),
    )

    view = await service._effective_control_view(record_from_card(card))

    assert view["state"] == "active"
    assert view["resolution"] == {
        "participant_card_revision": 3,
        "participant_catalog_version": "catalog-v1",
        "control_revision": 2,
        "control_basis_access_id": "agent-card-1",
        "control_basis_card_revision": 3,
        "control_basis_catalog_version": "catalog-v1",
    }


@pytest.mark.asyncio
async def test_live_resolution_fails_closed_when_control_projection_is_missing_or_updating() -> None:
    redis = _Redis()
    control = _control()
    card = _bound_card(control)
    _put_card(redis, card)

    with pytest.raises(LiveGrantCardError) as missing:
        await resolve_live_grant_card(
            redis,
            tenant="tenant",
            project="project",
            access_id=card.access_id,
        )
    assert missing.value.reason == "control_card_projection_missing"

    control_key = ControlCardRuntimeCache(
        redis,
        tenant="tenant",
        project="project",
    ).key(control.control_id)
    redis.values[control_key] = encode_cache_value(
        {
            "kind": CONTROL_CACHE_KIND_UPDATING,
            "revision": control.revision,
            "mutation_id": "mutation-1",
        }
    )
    with pytest.raises(LiveGrantCardError) as updating:
        await resolve_live_grant_card(
            redis,
            tenant="tenant",
            project="project",
            access_id=card.access_id,
        )
    assert updating.value.reason == "control_card_updating"

    _put_control(redis, control)
    effective = await resolve_live_grant_card(
        redis,
        tenant="tenant",
        project="project",
        access_id=card.access_id,
    )
    assert effective is not None
    assert effective.resource_operations[RESOURCE] == (
        "object.action.post_message",
    )


@pytest.mark.asyncio
async def test_only_one_project_control_can_attach_and_detach_restores_original_card() -> None:
    redis = _Redis()
    persistence = _Persistence(_card())
    control = _control()
    _put_control(redis, control)
    service = AutomationAccessService(
        redis=redis,
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=persistence,
    )
    service.notify_change = AsyncMock()

    attached = await service.attach_project_control(
        {"user_id": OWNER},
        access_id=persistence.authority.access_id,
        control_id=control.control_id,
    )
    assert attached["ok"] is True
    assert persistence.authority.control_card is not None
    assert persistence.authority.resource_operations == _card().resource_operations

    other = dataclasses.replace(
        control,
        control_id="project-control-other",
        issuer_ref="work:project:other",
    )
    _put_control(redis, other)
    refused = await service.attach_project_control(
        {"user_id": OWNER},
        access_id=persistence.authority.access_id,
        control_id=other.control_id,
    )
    assert refused["error"] == "project_control_already_attached"

    detached = await service.detach_project_control(
        {"user_id": OWNER},
        access_id=persistence.authority.access_id,
        control_id=control.control_id,
    )
    assert detached["ok"] is True
    assert persistence.authority.control_card is None
    assert persistence.authority.resource_operations == _card().resource_operations


@pytest.mark.asyncio
async def test_detach_never_resurrects_a_revoked_card() -> None:
    control = _control()
    revoked = replace_state(_bound_card(control), "revoked")
    persistence = _Persistence(revoked)
    service = AutomationAccessService(
        redis=_Redis(),
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=persistence,
    )

    result = await service.detach_project_control(
        {"user_id": OWNER},
        access_id=revoked.access_id,
        control_id=control.control_id,
    )

    assert result["error"] == "delegated_access_not_active"
    assert persistence.persist_calls == 0
    assert persistence.authority.state == "revoked"


@pytest.mark.asyncio
async def test_failed_store_transition_can_restore_only_its_previous_projection() -> None:
    redis = AsyncMock()
    redis.eval.return_value = 1
    cache = ControlCardRuntimeCache(redis, tenant="tenant", project="project")
    previous = _control()

    restored = await cache.rollback_transition(
        previous,
        mutation_id="mutation-1",
    )

    assert restored is True
    arguments = redis.eval.await_args.args
    assert arguments[1:3] == (1, cache.key(previous.control_id))
    payload = decode_cache_value(arguments[3])
    assert payload == {
        "kind": CONTROL_CACHE_KIND_CARD,
        "revision": previous.revision,
        "authority": previous.to_dict(),
    }
    assert arguments[4] == "mutation-1"

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    card_authority_from_record,
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
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
    CardCredentialHandles,
    ControlCardBinding,
    NamedServiceSelection,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.cards.service import (
    CardConflict,
    replace_state,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_PROPERTY,
    CONTROL_SNAPSHOT_SCHEMA,
    CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED,
    control_snapshot_is_exact,
    control_snapshot_metadata,
)
from connection_hub.delegated_credentials.controls.cache import (
    CONTROL_CACHE_KIND_CARD,
    CONTROL_CACHE_KIND_UPDATING,
    ControlCardRuntimeCache,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.model import (
    ProjectControlCardAuthority,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    ResourceAcceptance,
)
from connection_hub.delegated_credentials.live_grant import (
    LiveGrantCardError,
    resolve_live_grant_card,
    resolve_live_grant_composition,
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
    def __init__(
        self,
        authority: CardAuthority,
        *,
        initial: CardAuthority | None = None,
    ) -> None:
        self.authority = authority
        self.initial = initial or authority
        self.handles = CardCredentialHandles(access_id=authority.access_id)
        self.persist_calls = 0

    def _owned(self, subject_hash: str) -> bool:
        return subject_hash_for(self.authority.grantor_subject) == subject_hash

    async def load(self, access_id: str, *, subject_hash: str):
        if (
            access_id != self.authority.access_id
            or not self._owned(subject_hash)
            or self.authority.state != CARD_STATE_ACTIVE
            or (
                not authority_is_credentialless(self.authority)
                and self.authority.expires_at <= int(time.time())
            )
        ):
            return None
        return self.authority, self.handles

    async def load_current(self, access_id: str, *, subject_hash: str):
        if access_id != self.authority.access_id or not self._owned(subject_hash):
            return None
        return self.authority, self.handles

    async def load_initial(self, access_id: str, *, subject_hash: str):
        if access_id != self.initial.access_id or not self._owned(subject_hash):
            return None
        return self.initial

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


def _regular_control(
    *,
    operations: tuple[str, ...] = ("object.action.post_message",),
    composition_mode: str = CONTROL_COMPOSITION_AND,
    revision: int = 2,
) -> CardAuthority:
    control = new_credentialless_card(
        initial_selection=_card(),
        grantor_subject=OWNER,
        catalog_version="catalog-v1",
        control_id="control-regular",
        issuer_ref=PROJECT_REF,
        issuer_kind="application",
        issuer_label="Demo project",
        manage_url="/problem-board/projects/demo/control",
        composition_mode=composition_mode,
        now=int(time.time()),
    )
    claims = (
        ("slack:post",)
        if operations == ("object.action.post_message",)
        else ("slack:files:write",)
    )
    return dataclasses.replace(
        control,
        card_revision=revision,
        resource_grants={RESOURCE: ("named_services:use",)},
        resource_operations={RESOURCE: operations},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"slack": operations}}
        ),
        account_scope={"slack": {"workspace-1": claims}},
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


def test_v4_card_remains_readable_and_current_card_round_trips_one_control_binding() -> None:
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


def test_regular_control_card_round_trips_as_credentialless_and_defaults_to_and() -> None:
    control = new_credentialless_card(
        initial_selection=_card(),
        grantor_subject=OWNER,
        catalog_version="catalog-v1",
        control_id="control-regular",
        issuer_ref=PROJECT_REF,
        issuer_kind="application",
        properties={"coordination": {"version_control": {"model": "shared-main"}}},
        now=int(time.time()),
    )

    restored = CardAuthority.from_mapping(control.to_dict())

    assert authority_is_credentialless(restored)
    assert restored.delegate_subject == ""
    assert restored.expires_at == 0
    assert restored.identity_scope == "grantor"
    assert restored.composition_mode == CONTROL_COMPOSITION_AND
    assert restored.properties == control.properties


def test_control_card_initial_selection_preserves_authority_properties() -> None:
    seed = dataclasses.replace(
        _card(),
        properties={
            "kdcube.application_operations": {
                "schema": "kdcube.application_operations.v1",
                "mode": "selected",
            },
            "source-only": True,
        },
    )
    control = new_credentialless_card(
        initial_selection=seed,
        grantor_subject=OWNER,
        catalog_version="catalog-v1",
        control_id="control-regular",
        issuer_ref=PROJECT_REF,
        issuer_kind="application",
        properties={"source-only": False, "coordination": {"project": "demo"}},
        now=int(time.time()),
    )

    assert {
        key: value
        for key, value in control.properties.items()
        if key != CONTROL_SNAPSHOT_PROPERTY
    } == {
        "kdcube.application_operations": {
            "schema": "kdcube.application_operations.v1",
            "mode": "selected",
        },
        "source-only": False,
        "coordination": {"project": "demo"},
    }
    assert control_snapshot_is_exact(control)
    assert control_snapshot_metadata(control.properties) == {
        "schema": "connection_hub.control_snapshot.v1",
        "mode": "exact",
        "state": "exact",
        "basis_catalog_version": "catalog-v1",
        "origin": "created",
        "source_card_revision": 3,
    }


@pytest.mark.asyncio
async def test_legacy_control_migrates_from_first_revision_not_current_catalog() -> None:
    initial = dataclasses.replace(
        _regular_control(operations=("project.plan.item",), revision=1),
        properties={},
        catalog_version="catalog-before-review",
    )
    widened = dataclasses.replace(
        initial,
        card_revision=9,
        catalog_version="catalog-with-review",
        resource_operations={
            RESOURCE: ("project.plan.item", "review.accept", "review.return")
        },
    )
    persistence = _Persistence(widened, initial=initial)
    service = AutomationAccessService(
        redis=_Redis(),
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=persistence,
    )

    migrated = await service._ensure_control_snapshot(record_from_card(widened))

    assert migrated.card_revision == 10
    assert migrated.catalog_version == "catalog-before-review"
    assert migrated.resource_operations == {RESOURCE: ("project.plan.item",)}
    assert "review.accept" not in migrated.operations
    assert control_snapshot_is_exact(card_authority_from_record(migrated))
    assert control_snapshot_metadata(migrated.properties)["source_card_revision"] == 1
    assert persistence.persist_calls == 1


@pytest.mark.asyncio
async def test_historical_wildcard_is_materialized_only_from_historical_evidence() -> None:
    initial = dataclasses.replace(
        _regular_control(revision=1),
        properties={},
        catalog_version="catalog-before-upload",
        resource_grants={RESOURCE: ("*",)},
        resource_operations={RESOURCE: ("*",)},
        named_service_operations=NamedServiceSelection.all(),
        named_services={
            "namespaces": {
                "slack": {
                    "tools": {
                        "post": {
                            "operations": {
                                "object.action.post_message": {
                                    "grants": ["slack:post"]
                                }
                            }
                        }
                    }
                }
            }
        },
        resource_acceptance={
            RESOURCE: ResourceAcceptance(
                kind="catalog",
                revision="catalog-before-upload",
                digest="a" * 64,
                grants=("named_services:use",),
                operations={"object.action.post_message": "b" * 64},
            )
        },
    )
    widened = dataclasses.replace(
        initial,
        card_revision=6,
        catalog_version="catalog-with-upload",
        resource_operations={RESOURCE: ("*",)},
        named_services=_named_services(),
    )
    persistence = _Persistence(widened, initial=initial)
    service = AutomationAccessService(
        redis=_Redis(),
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=persistence,
    )

    migrated = await service._ensure_control_snapshot(record_from_card(widened))

    assert migrated.resource_grants == {RESOURCE: ("named_services:use",)}
    assert migrated.resource_operations == {
        RESOURCE: ("object.action.post_message",)
    }
    assert migrated.named_service_operations.operations == {
        RESOURCE: {"slack": ("object.action.post_message",)}
    }
    assert "object.action.upload_file" not in str(migrated.to_public_dict())


@pytest.mark.asyncio
async def test_legacy_control_without_history_becomes_editable_deny_all() -> None:
    legacy = dataclasses.replace(
        _regular_control(revision=4),
        properties={},
        resource_grants={RESOURCE: ("*",)},
        resource_operations={RESOURCE: ("*",)},
        named_service_operations=NamedServiceSelection.all(),
        account_scope={"*": {"*": ("*",)}},
    )
    persistence = _Persistence(legacy)
    persistence.initial = dataclasses.replace(legacy, access_id="other-control")
    service = AutomationAccessService(
        redis=_Redis(),
        tenant="tenant",
        project="project",
        config=None,
        grant_store=object(),
        card_persistence=persistence,
    )

    migrated = await service._ensure_control_snapshot(record_from_card(legacy))

    assert migrated.resource_grants == {}
    assert migrated.resource_operations == {}
    assert migrated.named_service_operations.is_none
    assert migrated.account_scope == {}
    metadata = control_snapshot_metadata(migrated.properties)
    assert metadata["state"] == CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED
    assert metadata["review_required"] == ["historical_boundary_unavailable"]


def test_runtime_refuses_an_unmarked_or_wildcard_control_snapshot() -> None:
    exact = _regular_control()
    caller = dataclasses.replace(
        _card(),
        control_card=ControlCardBinding(
            control_id=exact.access_id,
            issuer_ref=exact.issuer_ref,
            issuer_kind=exact.issuer_kind,
            control_revision=exact.card_revision,
        ),
    )
    unmarked = dataclasses.replace(exact, properties={})
    with pytest.raises(ControlCardMismatch) as missing_marker:
        effective_card_authority(caller, unmarked)
    assert missing_marker.value.reason == "control_card_exact_snapshot_required"

    wildcard = dataclasses.replace(
        exact,
        resource_operations={RESOURCE: ("*",)},
    )
    with pytest.raises(ControlCardMismatch) as open_ended:
        effective_card_authority(caller, wildcard)
    assert open_ended.value.reason == "control_card_exact_snapshot_required"

    for incomplete_metadata in (
        {"schema": CONTROL_SNAPSHOT_SCHEMA, "mode": "exact", "state": "exact"},
        {
            "schema": CONTROL_SNAPSHOT_SCHEMA,
            "mode": "exact",
            "state": "unknown",
            "basis_catalog_version": "catalog-v1",
        },
    ):
        incomplete = dataclasses.replace(
            exact,
            properties={CONTROL_SNAPSHOT_PROPERTY: incomplete_metadata},
        )
        with pytest.raises(ControlCardMismatch) as invalid_metadata:
            effective_card_authority(caller, incomplete)
        assert invalid_metadata.value.reason == "control_card_exact_snapshot_required"


def test_effective_authority_refuses_a_different_identity_scope() -> None:
    control = dataclasses.replace(
        _regular_control(composition_mode=CONTROL_COMPOSITION_OR),
        identity_scope="service-account",
    )
    caller = dataclasses.replace(
        _card(),
        identity_scope="grantor",
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )

    with pytest.raises(ControlCardMismatch) as mismatch:
        effective_card_authority(caller, control)

    assert mismatch.value.reason == "control_card_identity_scope_mismatch"


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
        "control_card_revision": 2,
        "control_catalog_version": "catalog-v1",
    }
    assert view["control_authority"]["resource_operations"] == {
        RESOURCE: ["object.action.post_message"]
    }
    assert "access_token" not in view["control_authority"]
    assert "refresh_token" not in view["control_authority"]


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
    assert missing.value.reason == "control_card_unresolvable"

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
async def test_live_resolution_fails_closed_when_regular_control_projection_is_missing() -> None:
    redis = _Redis()
    control = _regular_control()
    card = dataclasses.replace(
        _card(),
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )
    _put_card(redis, card)

    with pytest.raises(LiveGrantCardError) as missing:
        await resolve_live_grant_card(
            redis,
            tenant="tenant",
            project="project",
            access_id=card.access_id,
        )

    assert missing.value.reason == "control_card_unresolvable"


@pytest.mark.asyncio
async def test_live_resolution_fails_closed_when_control_identity_scope_changes() -> None:
    redis = _Redis()
    control = dataclasses.replace(
        _regular_control(composition_mode=CONTROL_COMPOSITION_OR),
        identity_scope="service-account",
    )
    card = dataclasses.replace(
        _card(),
        identity_scope="grantor",
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )
    _put_card(redis, card)
    _put_card(redis, control)

    with pytest.raises(LiveGrantCardError) as mismatch:
        await resolve_live_grant_card(
            redis,
            tenant="tenant",
            project="project",
            access_id=card.access_id,
        )

    assert mismatch.value.reason == "control_card_identity_scope_mismatch"


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
    assert refused["error"] == "control_card_already_attached"

    detached = await service.detach_project_control(
        {"user_id": OWNER},
        access_id=persistence.authority.access_id,
        control_id=control.control_id,
    )
    assert detached["ok"] is True
    assert persistence.authority.control_card is None
    assert persistence.authority.resource_operations == _card().resource_operations


@pytest.mark.asyncio
async def test_unbound_card_passes_through_and_control_mode_changes_apply_next_call() -> None:
    redis = _Redis()
    caller = dataclasses.replace(
        _card(),
        resource_operations={RESOURCE: ("object.action.post_message",)},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"slack": ("object.action.post_message",)}}
        ),
        account_scope={"slack": {"workspace-1": ("slack:post",)}},
    )
    _put_card(redis, caller)

    unchanged = await resolve_live_grant_card(
        redis,
        tenant="tenant",
        project="project",
        access_id=caller.access_id,
    )
    assert unchanged == caller

    control_and = _regular_control(
        operations=("object.action.upload_file",),
        composition_mode=CONTROL_COMPOSITION_AND,
    )
    bound = dataclasses.replace(
        caller,
        control_card=ControlCardBinding(
            control_id=control_and.access_id,
            issuer_ref=control_and.issuer_ref,
            issuer_kind=control_and.issuer_kind,
            control_revision=control_and.card_revision,
        ),
    )
    _put_card(redis, bound)
    _put_card(redis, control_and)

    narrowed_composition = await resolve_live_grant_composition(
        redis,
        tenant="tenant",
        project="project",
        access_id=caller.access_id,
    )
    assert narrowed_composition is not None
    assert narrowed_composition.caller_card == bound
    assert narrowed_composition.control_card == control_and
    narrowed = narrowed_composition.effective_card
    assert narrowed is not None
    assert narrowed.resource_operations[RESOURCE] == ()

    control_or = dataclasses.replace(
        control_and,
        card_revision=control_and.card_revision + 1,
        composition_mode=CONTROL_COMPOSITION_OR,
    )
    _put_card(redis, control_or)
    expanded = await resolve_live_grant_card(
        redis,
        tenant="tenant",
        project="project",
        access_id=caller.access_id,
    )
    assert expanded is not None
    assert expanded.resource_operations[RESOURCE] == (
        "object.action.post_message",
        "object.action.upload_file",
    )
    assert expanded.account_scope["slack"]["workspace-1"] == (
        "slack:files:write",
        "slack:post",
    )

    control_and_again = dataclasses.replace(
        control_or,
        card_revision=control_or.card_revision + 1,
        composition_mode=CONTROL_COMPOSITION_AND,
    )
    _put_card(redis, control_and_again)
    narrowed_again = await resolve_live_grant_card(
        redis,
        tenant="tenant",
        project="project",
        access_id=caller.access_id,
    )
    assert narrowed_again is not None
    assert narrowed_again.resource_operations[RESOURCE] == ()
    assert "object.action.upload_file" not in str(narrowed_again.named_services)


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

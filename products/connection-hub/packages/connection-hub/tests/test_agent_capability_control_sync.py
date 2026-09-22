from __future__ import annotations

import time

import pytest

from connection_hub.delegated_credentials import agent_capability_sync
from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_ACCEPTANCE_KIND,
)
from connection_hub.delegated_credentials.agent_capability_sync import (
    AGENT_CAPABILITY_CARD_LEASE_SECONDS,
)
from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_POLICY_SCHEMA,
    CAPABILITY_ALLOWED_SELECTED,
    CAPABILITY_ALLOWED_UNSELECTED,
    CAPABILITY_NOT_ALLOWED,
)
from connection_hub.delegated_credentials.application_resources import (
    application_resource,
)
from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_OPERATIONS_PROPERTY,
)
from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    CardCredentialHandles,
    authority_is_credentialless,
    authority_is_usable,
)
from connection_hub.delegated_credentials.cards.service import CardConflict
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    conversation_targets,
)
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)

TENANT = "tenant-a"
PROJECT = "project-a"
APPLICATION = "problem-board@1-0"
AGENT = "worker"
OWNER = "user-1"
RESOURCE = application_resource(
    tenant=TENANT,
    project=PROJECT,
    application=APPLICATION,
    agent=AGENT,
)


class _Persistence:
    def __init__(self) -> None:
        self.records: dict[str, tuple[CardAuthority, CardCredentialHandles]] = {}
        self.persisted: list[str] = []

    def _owned(self, authority: CardAuthority, subject_hash: str) -> bool:
        return subject_hash_for(authority.grantor_subject) == subject_hash

    async def load(self, access_id: str, *, subject_hash: str):
        held = self.records.get(access_id)
        if held is None:
            return None
        authority, handles = held
        if (
            not self._owned(authority, subject_hash)
            or authority.state != CARD_STATE_ACTIVE
            or (
                not authority_is_credentialless(authority)
                and authority.expires_at <= int(time.time())
            )
        ):
            return None
        return authority, handles

    async def load_current(self, access_id: str, *, subject_hash: str):
        held = self.records.get(access_id)
        if held is None or not self._owned(held[0], subject_hash):
            return None
        return held

    async def current_revision(self, access_id: str, *, subject_hash: str) -> int:
        held = await self.load_current(access_id, subject_hash=subject_hash)
        return held[0].card_revision if held is not None else 0

    async def persist(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
        *,
        subject_hash: str,
        expected_revision: int,
    ) -> None:
        if subject_hash_for(authority.grantor_subject) != subject_hash:
            raise AssertionError("wrong owner")
        current = self.records.get(authority.access_id)
        current_revision = current[0].card_revision if current is not None else 0
        if expected_revision != current_revision:
            raise CardConflict(
                "card_revision_moved",
                current_revision=current_revision,
            )
        self.records[authority.access_id] = (authority, handles)
        self.persisted.append(authority.access_id)


def _policy(*tools: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {"tools": list(tools)},
    }


def _policy_with_targets(*targets: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {"conversation_targets": list(targets)},
    }


def _service(
    *, application_apis: bool = False
) -> tuple[AutomationAccessService, _Persistence]:
    resources = (
        [
            {
                "resource": "*",
                "label": "Application APIs",
                "grants": ["kdcube:role:super-admin"],
            }
        ]
        if application_apis
        else []
    )
    document = CatalogDocument.build(
        {"delegated_credentials": {"oauth": {"enabled": True, "resources": resources}}}
    )

    class _Resolver:
        async def resolve_active(self):
            return document

    persistence = _Persistence()
    service = AutomationAccessService(
        redis=None,
        tenant=TENANT,
        project=PROJECT,
        config=oauth_delegated_config_from_connections(document.connections),
        catalog_resolver=_Resolver(),
        card_persistence=persistence,
    )
    return service, persistence


async def _sync(
    service: AutomationAccessService,
    *,
    revision: str,
    authority: tuple[str, ...],
    catalog: tuple[str, ...],
    selection: tuple[str, ...] | None = None,
    replace_selection: bool = False,
) -> dict:
    return await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision=revision,
        descriptor_payload={"revision": revision, "tools": list(catalog)},
        capability_authority=_policy(*authority),
        capability_catalog=_policy(*catalog),
        selected_capabilities=(_policy(*selection) if selection is not None else None),
        replace_selection=replace_selection,
        conversation_target_resources=(
            application_resource(
                tenant=TENANT,
                project=PROJECT,
                application="*",
                agent="*",
            ),
        ),
        issuer_label="Problem Board worker",
    )


@pytest.mark.asyncio
async def test_descriptor_sync_is_stable_and_new_capabilities_are_unselected() -> None:
    service, persistence = _service()

    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.blocked"),
        selection=("tool.old", "tool.blocked"),
    )

    assert created["ok"] is True
    assert created["control_changed"] is True
    assert created["card_changed"] is True
    assert created["projection"]["capabilities"] == {"tools": ["tool.old"]}
    assert created["states"] == {
        "tools": {
            "tool.blocked": CAPABILITY_NOT_ALLOWED,
            "tool.new": CAPABILITY_ALLOWED_UNSELECTED,
            "tool.old": CAPABILITY_ALLOWED_SELECTED,
        }
    }
    assert len(persistence.records) == 2
    assert len(persistence.persisted) == 2
    resident_id = created["card"]["access_id"]
    control_id = created["control_card"]["access_id"]
    resident = persistence.records[resident_id][0]
    assert resident.control_card is not None
    assert resident.control_card.control_id == control_id
    assert resident.resource_acceptance[RESOURCE].kind == (
        AGENT_DESCRIPTOR_ACCEPTANCE_KIND
    )

    replay = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.blocked"),
    )

    assert replay["ok"] is True
    assert replay["control_changed"] is False
    assert replay["card_changed"] is False
    assert len(persistence.persisted) == 2

    added = await _sync(
        service,
        revision="descriptor-r2",
        authority=("tool.old", "tool.new", "tool.future"),
        catalog=("tool.old", "tool.new", "tool.future", "tool.blocked"),
    )

    assert added["control_changed"] is True
    assert added["card_changed"] is False
    assert added["states"]["tools"]["tool.future"] == (CAPABILITY_ALLOWED_UNSELECTED)
    assert added["projection"]["capabilities"] == {"tools": ["tool.old"]}


@pytest.mark.asyncio
async def test_expired_capability_lease_renews_the_same_card_and_selection(
    monkeypatch,
) -> None:
    service, persistence = _service()
    created_at = 2_000_000_000
    monkeypatch.setattr(agent_capability_sync.time, "time", lambda: created_at)
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )
    access_id = created["card"]["access_id"]
    first_revision = created["card"]["card_revision"]
    first_authority, first_handles = persistence.records[access_id]
    assert first_handles.empty is True
    assert created["card"]["expires_at"] == (
        created_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS
    )

    renewed_at = created_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS + 1
    assert authority_is_usable(first_authority, renewed_at) is False
    monkeypatch.setattr(agent_capability_sync.time, "time", lambda: renewed_at)
    renewed = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
    )

    assert renewed["card_changed"] is True
    assert renewed["card"]["access_id"] == access_id
    assert renewed["card"]["card_revision"] == first_revision + 1
    assert renewed["card"]["expires_at"] == (
        renewed_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS
    )
    assert renewed["selection"]["capabilities"] == {"tools": ["tool.old"]}
    assert renewed["projection"]["capabilities"] == {"tools": ["tool.old"]}
    renewed_authority, renewed_handles = persistence.records[access_id]
    assert authority_is_usable(renewed_authority, renewed_at) is True
    assert renewed_handles.empty is True


@pytest.mark.asyncio
async def test_effective_conversation_targets_come_from_capability_projection() -> None:
    service, persistence = _service()
    target = application_resource(
        tenant=TENANT,
        project=PROJECT,
        application="*",
        agent="*",
    )

    selected = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_targets(target),
        capability_catalog=_policy_with_targets(target),
        selected_capabilities=_policy_with_targets(target),
        replace_selection=True,
        conversation_target_resources=(target,),
    )

    resident = persistence.records[selected["card"]["access_id"]][0]
    control = persistence.records[selected["control_card"]["access_id"]][0]
    effective = effective_card_authority(resident, control)

    assert selected["projection"]["capabilities"] == {
        "conversation_targets": [target]
    }
    assert conversation_targets(effective.properties) == (target,)

    cleared = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_targets(target),
        capability_catalog=_policy_with_targets(target),
        selected_capabilities=_policy_with_targets(),
        replace_selection=True,
        conversation_target_resources=(target,),
    )
    effective = effective_card_authority(
        persistence.records[cleared["card"]["access_id"]][0],
        persistence.records[cleared["control_card"]["access_id"]][0],
    )
    assert conversation_targets(effective.properties) == ()


@pytest.mark.asyncio
async def test_removed_capability_is_hidden_then_restored_from_user_selection() -> None:
    service, _persistence = _service()
    await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )

    removed = await _sync(
        service,
        revision="descriptor-r2",
        authority=("tool.new",),
        catalog=("tool.old", "tool.new"),
    )
    assert removed["selection"]["capabilities"] == {"tools": ["tool.old"]}
    assert removed["projection"]["capabilities"] == {"tools": []}
    assert removed["states"]["tools"]["tool.old"] == CAPABILITY_NOT_ALLOWED

    restored = await _sync(
        service,
        revision="descriptor-r3",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
    )
    assert restored["projection"]["capabilities"] == {"tools": ["tool.old"]}
    assert restored["states"]["tools"]["tool.old"] == (CAPABILITY_ALLOWED_SELECTED)


@pytest.mark.asyncio
async def test_picker_replacement_changes_visible_entries_only() -> None:
    service, _persistence = _service()
    await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.old", "tool.hidden"),
    )

    changed = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.new", "tool.outside"),
        replace_selection=True,
    )

    assert changed["control_changed"] is False
    assert changed["card_changed"] is True
    assert changed["selection"]["capabilities"] == {
        "tools": ["tool.hidden", "tool.new"]
    }
    assert changed["projection"]["capabilities"] == {"tools": ["tool.new"]}

    replay = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.new", "tool.outside"),
        replace_selection=True,
    )
    assert replay["card_changed"] is False


@pytest.mark.asyncio
async def test_descriptor_ceiling_does_not_require_the_user_to_hold_its_roles() -> None:
    service, persistence = _service(application_apis=True)
    operation = (
        "urn:kdcube:application-operation:problem-board%401-0:"
        "api.operations.post.agent_capabilities"
    )

    result = await service.sync_agent_capability_control(
        {"user_id": OWNER, "roles": []},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy("tool.old"),
        selected_capabilities=_policy("tool.old"),
        resource_grants={"*": ["kdcube:role:registered"]},
        resource_operations={"*": [operation]},
        properties={
            APPLICATION_OPERATIONS_PROPERTY: {
                "schema": "kdcube.application_operations.v2",
                "mode": "selected",
                "default_role": "kdcube:role:registered",
                "operation_roles": {},
            }
        },
    )

    assert result["ok"] is True
    control = persistence.records[result["control_card"]["access_id"]][0]
    assert control.resource_grants == {"*": ("kdcube:role:registered",)}
    assert control.resource_operations == {"*": (operation,)}

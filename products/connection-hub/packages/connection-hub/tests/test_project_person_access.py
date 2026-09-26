# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.effective import (
    intersect_card_authority_selection,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    materialize_control_snapshot,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_READ,
    PROJECT_PERSON_CONTROL_REVOKE,
    PROJECT_PERSON_CONTROL_UPDATE,
    PROJECT_PERSON_MY_CARD_SEED,
    ProjectAuthorizationDecision,
)
from connection_hub.delegated_credentials.project_person_access import (
    PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE,
    PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA,
    PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE,
    PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA,
    PROJECT_PERSON_MY_CARD_SEED_PROVENANCE,
    ProjectPersonControlLifecycle,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    PROJECT_IDENTITY_EDGE_PROVENANCE,
    ProjectPersonCardIdentity,
)
from connection_hub.delegated_credentials.project_identity_authorization import (
    ProjectOperationRequest,
)


PROJECT_REF = "work:project:quickstart"
ADMIN = "platform-admin-1"
TARGET = "platform-user-2"
RESOURCE = "https://board.example.test/mcp"
OPERATION = "review.accept"
GRANT = "work:review"


@dataclass(frozen=True)
class _Record:
    authority: CardAuthority

    def __getattr__(self, name: str) -> Any:
        return getattr(self.authority, name)

    def to_public_dict(self) -> dict[str, Any]:
        return self.authority.to_dict()


class _Port:
    def __init__(
        self, *, deny_actor: str = "", mismatch: bool = False, deny_operations: frozenset = frozenset()
    ) -> None:
        self.deny_actor = deny_actor
        self.mismatch = mismatch
        # Refuse only these operations (a member: reads their own, writes nothing).
        self.deny_operations = deny_operations
        self.requests = []

    async def authorize_project_person_control(self, request):
        self.requests.append(request)
        if request.actor_subject == self.deny_actor:
            return ProjectAuthorizationDecision.deny(
                request,
                reason="project_person_control_admin_required",
            )
        if request.operation in self.deny_operations:
            return ProjectAuthorizationDecision.deny(
                request,
                reason="project_person_control_decided_by_admin",
            )
        decision = ProjectAuthorizationDecision.allow(
            request,
            delegable_grants=("work:admin", "work:review"),
            platform_admin=True,
            evidence={"membership_revision": 7},
        )
        if self.mismatch:
            return dataclasses.replace(decision, target_subject="platform-user-3")
        return decision


class _Host:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[_Record, str]] = {}
        self.notifications: list[tuple[str, str]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.forget_calls: list[tuple[_Record, _Record]] = []

    async def _load_record_any_state(self, access_id, *, grantor_subject):
        return self.records.get((grantor_subject, access_id))

    async def _load_record(self, access_id, *, grantor_subject):
        loaded = self.records.get((grantor_subject, access_id))
        if loaded is None or loaded[1] != CARD_STATE_ACTIVE:
            return None
        return loaded[0]

    async def _ensure_control_snapshot(self, record):
        return record

    async def _active_catalog(self):
        return CatalogDocument.build(
            {
                "delegated_credentials": {
                    "oauth": {
                        "resources": [
                            {
                                "resource": RESOURCE,
                                "grants": [GRANT],
                                "tools": {OPERATION: {"grants": [GRANT]}},
                            }
                        ]
                    }
                }
            }
        )

    @staticmethod
    def _version_of(active):
        return active.version

    async def _catalog_config(self, active, *, owner_subject):
        del active, owner_subject
        return SimpleNamespace(resources=())

    async def _persist_record(self, record, *, expected_revision):
        assert expected_revision == 0
        key = (record.grantor_subject, record.access_id)
        assert key not in self.records
        self.records[key] = (record, CARD_STATE_ACTIVE)

    async def _control_card_public_view(
        self,
        user,
        record,
        *,
        state,
        _delegable_grants,
        _platform_admin,
    ):
        return {
            **record.to_public_dict(),
            "state": state,
            "catalog_drift": {},
            "resource_offers": [],
            "view_context": {
                "user_id": user["user_id"],
                "delegable_grants": list(_delegable_grants),
                "platform_admin": _platform_admin,
            },
        }

    async def _resolve_card_authority(self, **kwargs):
        self.resolve_call = kwargs
        return SimpleNamespace(
            error={"ok": False, "error": "selection_resolution_reached"},
        )

    async def notify_change(self, subject, *, action, access=None, access_id=""):
        del access, access_id
        self.notifications.append((subject, action))

    async def update_access(self, user, **kwargs):
        self.update_calls.append({"user": user, **kwargs})
        key = (user["user_id"], kwargs["access_id"])
        existing, _state = self.records[key]
        assert kwargs.get("expected_card_revision") in (
            None,
            existing.card_revision,
        )
        resource_grants = kwargs.get("resource_grants", existing.resource_grants)
        resource_operations = kwargs.get(
            "resource_operations",
            existing.resource_operations,
        )
        named_service_operations = NamedServiceSelection.from_stored(
            kwargs.get(
                "named_service_operations",
                existing.named_service_operations.to_stored(),
            ),
            present=True,
        )
        candidate = _Record(
            dataclasses.replace(
                existing.authority,
                card_revision=existing.card_revision + 1,
                label=kwargs.get("label") or existing.label,
                operations=tuple(
                    sorted(
                        {
                            operation
                            for operations in resource_operations.values()
                            for operation in operations
                        }
                    )
                ),
                resource_grants={
                    resource: tuple(grants)
                    for resource, grants in resource_grants.items()
                },
                resource_operations={
                    resource: tuple(operations)
                    for resource, operations in resource_operations.items()
                },
                named_service_operations=named_service_operations,
                account_scope=kwargs.get("account_scope", existing.account_scope),
                properties=kwargs.get("properties", existing.properties),
            )
        )
        transform = kwargs["_record_transform"]
        updated = transform(existing, candidate)
        self.records[key] = (updated, CARD_STATE_ACTIVE)
        await self.notify_change(
            kwargs["_notification_subject"],
            action="updated",
            access=updated.to_public_dict(),
        )
        return {"ok": True, "access": updated.to_public_dict(), "pruned": {}}

    async def _forget_record(self, record, *, revoked_record):
        self.forget_calls.append((record, revoked_record))
        key = (record.grantor_subject, record.access_id)
        assert self.records[key][0] is record
        self.records[key] = (revoked_record, revoked_record.state)


class _ResolvedSelection:
    @staticmethod
    def to_public_dict() -> dict[str, Any]:
        return {"resources": [], "claims": [], "named_service_operations": []}


class _SelectionHost(_Host):
    @staticmethod
    def _configured_resource(resource, *, config):
        del resource, config
        return None

    async def _resolve_card_authority(self, **kwargs):
        resource_grants = {
            resource: tuple(grants)
            for resource, grants in dict(kwargs.get("resource_grants") or {}).items()
        }
        resource_operations = {
            resource: tuple(operations)
            for resource, operations in dict(
                kwargs.get("resource_operations") or {}
            ).items()
        }
        return SimpleNamespace(
            error=None,
            revoke=False,
            operations=tuple(
                sorted(
                    {
                        operation
                        for operations in resource_operations.values()
                        for operation in operations
                    }
                )
            ),
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            named_service_operations=NamedServiceSelection.none(),
            named_services={},
            account_scope=kwargs.get("account_scope") or {},
            identity_scope="grantor",
            properties=dict(kwargs.get("properties") or {}),
            reconciled=_ResolvedSelection(),
        )


def _lifecycle(host: _Host, port: Any) -> ProjectPersonControlLifecycle:
    return ProjectPersonControlLifecycle(
        host=host,
        authorization_port=port,
        authority_from_record=lambda record: record.authority,
        record_from_authority=_Record,
    )


async def _create(
    lifecycle: ProjectPersonControlLifecycle,
    *,
    actor: str = ADMIN,
    request_id: str = "request-create",
    migration: bool = False,
    project_creation: bool = False,
):
    return await lifecycle.create(
        actor_subject=actor,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id=request_id,
        label="Quickstart member",
        migration=migration,
        project_creation=project_creation,
    )


@pytest.mark.asyncio
async def test_missing_authorization_port_is_default_closed() -> None:
    host = _Host()

    result = await _create(_lifecycle(host, None))

    assert result == {
        "ok": False,
        "error": "project_person_control_authorization_unavailable",
        "reason": "authorization_port_not_configured",
        "retryable": True,
        "status": 503,
    }
    assert host.records == {}


@pytest.mark.asyncio
async def test_decision_for_another_target_is_refused_before_storage() -> None:
    host = _Host()

    result = await _create(_lifecycle(host, _Port(mismatch=True)))

    assert result["error"] == "project_person_control_authorization_invalid"
    assert result["reason"] == "project_authorization_target_mismatch"
    assert host.records == {}


@pytest.mark.asyncio
async def test_create_is_project_held_target_named_and_audited() -> None:
    host = _Host()
    port = _Port()

    result = await _create(_lifecycle(host, port))

    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    stored, state = host.records[(identity.project_subject, identity.control_id)]
    person_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    my_card, my_card_state = host.records[(TARGET, person_identity.my_card_id)]
    audit = stored.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert result["ok"] is True
    assert result["created"] is True
    assert state == CARD_STATE_ACTIVE
    assert stored.grantor_subject == identity.project_subject
    assert stored.grantor_subject != TARGET
    assert stored.properties[PROJECT_PERSON_CONTROL_PROPERTY]["target_subject"] == TARGET
    assert PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE not in stored.provenance
    assert PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE not in stored.provenance
    assert my_card_state == CARD_STATE_ACTIVE
    assert my_card.grantor_subject == TARGET
    assert my_card.delegate_subject == TARGET
    assert my_card.resource_grants == stored.resource_grants
    assert my_card.resource_operations == stored.resource_operations
    assert my_card.named_service_operations == stored.named_service_operations
    assert my_card.account_scope == stored.account_scope
    assert my_card.control_card is not None
    assert my_card.control_card.control_id == identity.control_id
    assert my_card.provenance[PROJECT_IDENTITY_EDGE_PROVENANCE]["edge_ref"] == (
        person_identity.edge_ref
    )
    assert result["my_card_created"] is True
    assert result["my_card"]["access_id"] == person_identity.my_card_id
    assert result["project_identity_edge"]["edge_ref"] == person_identity.edge_ref
    assert audit["action"] == "created"
    assert audit["actor_subject"] == ADMIN
    assert audit["request_id"] == "request-create"
    assert host.notifications == [(TARGET, "project_person_control_created")]
    assert port.requests[0].operation == PROJECT_PERSON_CONTROL_CREATE
    assert "creator" not in dataclasses.asdict(port.requests[0])


@pytest.mark.asyncio
async def test_ordinary_new_my_card_starts_equal_to_its_selected_control_card() -> None:
    host = _SelectionHost()
    lifecycle = _lifecycle(host, _Port())

    result = await lifecycle.create(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-create-selected",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
        account_scope={"slack": {"account-1": ["post"]}},
        label="Quickstart member",
    )

    control_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    person_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    control = host.records[
        (control_identity.project_subject, control_identity.control_id)
    ][0]
    my_card = host.records[(TARGET, person_identity.my_card_id)][0]
    assert result["ok"] is True
    assert my_card.operations == control.operations == (OPERATION,)
    assert my_card.resource_grants == control.resource_grants == {
        RESOURCE: (GRANT,)
    }
    assert my_card.resource_operations == control.resource_operations == {
        RESOURCE: (OPERATION,)
    }
    assert my_card.account_scope == control.account_scope == {
        "slack": {"account-1": ("post",)}
    }


@pytest.mark.asyncio
async def test_create_refuses_union_composition_before_storage() -> None:
    host = _Host()

    result = await _lifecycle(host, _Port()).create(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-create-or",
        composition_mode=CONTROL_COMPOSITION_OR,
        label="Quickstart member",
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_requires_and",
        "status": 400,
    }
    assert host.records == {}


@pytest.mark.asyncio
async def test_an_admin_edits_its_own_project_card_and_it_saves() -> None:
    """The operator's rule (W260): a project admin changes any Card, their own
    included. The board's Team > People writes it under that admin's session,
    so the actor is the target and the policy port decides."""

    host = _Host()
    await _create(_lifecycle(host, _Port()))
    host.update_calls.clear()
    admin_port = _Port()

    result = await _lifecycle(host, admin_port).update(
        actor_subject=TARGET,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-own-update",
        label="Wider",
    )

    assert result["ok"] is True, result
    assert len(host.update_calls) == 1
    assert [request.operation for request in admin_port.requests] == [PROJECT_PERSON_CONTROL_UPDATE]


@pytest.mark.asyncio
async def test_a_member_is_refused_its_own_project_card_by_the_policy() -> None:
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    host.update_calls.clear()
    member_port = _Port(deny_operations=frozenset({PROJECT_PERSON_CONTROL_UPDATE}))

    result = await _lifecycle(host, member_port).update(
        actor_subject=TARGET,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-own-update",
        label="Wider",
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_decided_by_admin",
        "status": 403,
    }
    assert host.update_calls == []


@pytest.mark.asyncio
async def test_update_refuses_union_composition_before_write() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle)
    host.update_calls.clear()

    result = await lifecycle.update(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-update-or",
        composition_mode=CONTROL_COMPOSITION_OR,
        expected_card_revision=1,
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_requires_and",
        "status": 400,
    }
    assert host.update_calls == []


@pytest.mark.asyncio
async def test_admin_update_uses_exact_decision_ceiling_and_appends_audit() -> None:
    host = _Host()
    port = _Port()
    lifecycle = _lifecycle(host, port)
    await _create(lifecycle)
    host.notifications.clear()

    result = await lifecycle.update(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-update",
        label="Narrowed member",
        expected_card_revision=1,
    )

    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    stored, _state = host.records[(identity.project_subject, identity.control_id)]
    call = host.update_calls[0]
    audit = stored.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert result["ok"] is True
    assert call["_delegable_grants"] == ("work:admin", "work:review")
    assert call["_platform_admin"] is True
    assert call["_notification_subject"] == TARGET
    assert call["user"] == {
        "user_id": identity.project_subject,
        "roles": [],
        "permissions": [],
    }
    assert audit["action"] == "updated"
    assert audit["actor_subject"] == ADMIN
    assert audit["request_id"] == "request-update"
    assert audit["changes"] == {
        "label": {
            "before": "Quickstart member",
            "after": "Narrowed member",
        }
    }
    assert host.notifications == [(TARGET, "updated")]


@pytest.mark.asyncio
async def test_creator_bootstrap_is_an_explicit_port_decision() -> None:
    host = _Host()
    port = _Port()

    result = await _create(
        _lifecycle(host, port),
        actor=TARGET,
        request_id="request-creator-bootstrap",
    )

    assert result["ok"] is True
    assert port.requests[0].actor_subject == TARGET
    assert port.requests[0].operation == PROJECT_PERSON_CONTROL_CREATE


@pytest.mark.asyncio
async def test_named_service_only_create_still_resolves_the_decision_ceiling() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())

    result = await lifecycle.create(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-named-service-create",
        named_service_operations={"press": ["read"]},
        label="Quickstart member",
    )

    assert result == {"ok": False, "error": "selection_resolution_reached"}
    assert host.resolve_call["named_service_operations"] == {
        "press": ["read"],
    }
    assert host.resolve_call["_delegable_grants"] == (
        "work:admin",
        "work:review",
    )


def _select_control(host: _Host) -> CardAuthority:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    stored, state = host.records[(identity.project_subject, identity.control_id)]
    selected = materialize_control_snapshot(
        dataclasses.replace(
            stored.authority,
            operations=(OPERATION,),
            resource_grants={RESOURCE: (GRANT,)},
            resource_operations={RESOURCE: (OPERATION,)},
        ),
        basis_catalog_version=stored.catalog_version,
        origin="updated",
    )
    host.records[(identity.project_subject, identity.control_id)] = (
        _Record(selected),
        state,
    )
    return selected


@pytest.mark.asyncio
async def test_seed_my_card_is_control_capped_one_shot_and_exactly_replayable() -> None:
    host = _Host()
    port = _Port()
    lifecycle = _lifecycle(host, port)
    await _create(lifecycle, migration=True)
    control = _select_control(host)
    identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    before, _state = host.records[(TARGET, identity.my_card_id)]

    first = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-seed-1",
        resource_grants={RESOURCE: ["work:admin", GRANT]},
        resource_operations={RESOURCE: ["review.return", OPERATION]},
    )
    replay = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-seed-retry",
        resource_grants={RESOURCE: [GRANT, "work:admin"]},
        resource_operations={RESOURCE: [OPERATION, "review.return"]},
    )
    conflict = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-seed-conflict",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
    )

    stored, state = host.records[(TARGET, identity.my_card_id)]
    marker = stored.provenance[PROJECT_PERSON_MY_CARD_SEED_PROVENANCE]
    control_marker = control.provenance[PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE]
    assert first["ok"] is True and first["seeded"] is True
    assert replay["ok"] is True and replay["seeded"] is False
    assert conflict == {
        "ok": False,
        "error": "project_person_my_card_seed_conflict",
        "status": 409,
    }
    assert state == CARD_STATE_ACTIVE
    assert stored.card_revision == 2
    assert stored.resource_grants == {RESOURCE: (GRANT,)}
    assert stored.resource_operations == {RESOURCE: (OPERATION,)}
    assert stored.operations == (OPERATION,)
    assert stored.label == before.label
    assert stored.properties == before.properties
    assert stored.control_card == before.control_card
    assert marker["control_id"] == control.access_id
    assert marker["control_revision"] == control.card_revision
    assert marker["origin"] == "migration"
    assert control_marker["schema"] == PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA
    assert control_marker["actor_subject"] == ADMIN
    assert control_marker["request_id"] == "request-create"
    assert port.requests[-3].operation == PROJECT_PERSON_MY_CARD_SEED
    assert len(host.update_calls) == 1


@pytest.mark.asyncio
async def test_project_creator_seed_uses_distinct_immutable_origin() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle, project_creation=True)
    control = _select_control(host)

    result = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-project-creator-seed",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
    )

    origin = control.provenance[PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE]
    assert result["ok"] is True
    assert result["seeded"] is True
    assert result["seed"]["origin"] == "project_creation"
    assert origin == {
        "schema": PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA,
        "actor_subject": ADMIN,
        "request_id": "request-create",
        "created_at": origin["created_at"],
    }


@pytest.mark.asyncio
async def test_control_create_refuses_two_seed_origins_before_authorization() -> None:
    host = _Host()
    port = _Port()

    result = await _create(
        _lifecycle(host, port),
        migration=True,
        project_creation=True,
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_seed_origin_conflict",
        "status": 400,
    }
    assert port.requests == []
    assert host.records == {}


@pytest.mark.asyncio
async def test_seed_refuses_a_my_card_the_person_already_changed() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle, migration=True)
    _select_control(host)
    identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    stored, state = host.records[(TARGET, identity.my_card_id)]
    host.records[(TARGET, identity.my_card_id)] = (
        _Record(dataclasses.replace(stored.authority, card_revision=2)),
        state,
    )

    result = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-seed-managed",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
    )

    assert result == {
        "ok": False,
        "error": "project_person_my_card_already_managed",
        "status": 409,
    }
    assert host.update_calls == []


@pytest.mark.asyncio
async def test_seed_refuses_a_normally_created_project_person() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle)
    _select_control(host)

    result = await lifecycle.seed_my_card(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-seed-not-migrated",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
    )

    assert result == {
        "ok": False,
        "error": "project_person_my_card_seed_not_migrated",
        "status": 409,
    }
    assert host.update_calls == []


def test_cross_owner_selection_cap_intersects_every_selection_dimension() -> None:
    named_services = {
        "namespaces": {
            "records": {
                "tools": {
                    "objects": {
                        "operations": {
                            "object.get": {"grants": [GRANT]},
                            "object.search": {"grants": [GRANT]},
                        }
                    }
                }
            }
        }
    }
    card = CardAuthority(
        access_id="my-card",
        client_id="person",
        grantor_subject=TARGET,
        delegate_subject=TARGET,
        source="oauth",
        card_kind="automation",
        resource_grants={RESOURCE: (GRANT, "work:admin")},
        resource_operations={RESOURCE: (OPERATION, "review.return")},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"records": ["object.get", "object.search"]}}
        ),
        named_services=named_services,
        account_scope={"records": {"account-1": [GRANT, "work:admin"]}},
    )
    ceiling = dataclasses.replace(
        card,
        access_id="control-card",
        grantor_subject="project",
        delegate_subject="",
        resource_grants={RESOURCE: (GRANT,)},
        resource_operations={RESOURCE: (OPERATION,)},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"records": ["object.search"]}}
        ),
        account_scope={"records": {"account-1": [GRANT]}},
    )

    capped = intersect_card_authority_selection(card, ceiling)

    assert capped.resource_grants == {RESOURCE: (GRANT,)}
    assert capped.resource_operations == {RESOURCE: (OPERATION,)}
    assert capped.named_service_operations.operations == {
        RESOURCE: {"records": ("object.search",)}
    }
    assert capped.account_scope == {"records": {"account-1": (GRANT,)}}


@pytest.mark.asyncio
async def test_project_operation_resolves_the_recorded_edge_and_both_current_cards() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle)
    control_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    person_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    control, control_state = host.records[
        (control_identity.project_subject, control_identity.control_id)
    ]
    my_card, my_card_state = host.records[(TARGET, person_identity.my_card_id)]
    selected = {
        "resource_grants": {RESOURCE: (GRANT,)},
        "resource_operations": {RESOURCE: (OPERATION,)},
    }
    host.records[(control_identity.project_subject, control_identity.control_id)] = (
        _Record(dataclasses.replace(control.authority, **selected)),
        control_state,
    )
    host.records[(TARGET, person_identity.my_card_id)] = (
        _Record(dataclasses.replace(my_card.authority, **selected)),
        my_card_state,
    )

    decision = await lifecycle.authorize_operation(
        ProjectOperationRequest(
            person_subject=TARGET,
            project_ref=PROJECT_REF,
            resource=RESOURCE,
            operation=OPERATION,
            required_grants=(GRANT,),
        )
    )

    assert decision.allowed is True
    assert decision.reason == "project_operation_allowed"
    assert decision.edge is not None
    assert decision.edge.edge_ref == person_identity.edge_ref
    assert decision.control_card is not None
    assert decision.my_card is not None


@pytest.mark.asyncio
async def test_project_operation_service_marks_allow_and_deny_as_evaluated() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle)
    control_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    person_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    control, control_state = host.records[
        (control_identity.project_subject, control_identity.control_id)
    ]
    my_card, my_card_state = host.records[(TARGET, person_identity.my_card_id)]
    selected = {
        "resource_grants": {RESOURCE: (GRANT,)},
        "resource_operations": {RESOURCE: (OPERATION,)},
    }
    host.records[(control_identity.project_subject, control_identity.control_id)] = (
        _Record(dataclasses.replace(control.authority, **selected)),
        control_state,
    )
    host.records[(TARGET, person_identity.my_card_id)] = (
        _Record(dataclasses.replace(my_card.authority, **selected)),
        my_card_state,
    )
    service = AutomationAccessService.__new__(AutomationAccessService)
    service._project_person_controls = lifecycle

    allowed = await service.project_operation_authorize(
        {"user_id": TARGET},
        project_ref=PROJECT_REF,
        resource=RESOURCE,
        operation=OPERATION,
        required_grants=(GRANT,),
    )
    denied = await service.project_operation_authorize(
        {"user_id": TARGET},
        project_ref=PROJECT_REF,
        resource=RESOURCE,
        operation="review.unknown",
        required_grants=(GRANT,),
    )

    assert allowed["ok"] is True
    assert allowed["allowed"] is True
    assert denied["ok"] is True
    assert denied["allowed"] is False


@pytest.mark.asyncio
async def test_project_operation_reports_malformed_control_through_evaluator() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle)
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    control, state = host.records[(identity.project_subject, identity.control_id)]
    malformed_properties = dict(control.properties)
    malformed_properties.pop(PROJECT_PERSON_CONTROL_PROPERTY)
    host.records[(identity.project_subject, identity.control_id)] = (
        _Record(
            dataclasses.replace(
                control.authority,
                properties=malformed_properties,
            )
        ),
        state,
    )

    decision = await lifecycle.authorize_operation(
        ProjectOperationRequest(
            person_subject=TARGET,
            project_ref=PROJECT_REF,
            resource=RESOURCE,
            operation=OPERATION,
            required_grants=(GRANT,),
        )
    )

    assert decision.allowed is False
    assert decision.reason == "control_card_identity_invalid"
    assert decision.blocking_boundary == "control_card"
    assert decision.details == {"identity_reason": "project_person_control_marker_missing"}


@pytest.mark.asyncio
async def test_admin_revoke_is_audited_and_removes_live_authority() -> None:
    host = _Host()
    port = _Port()
    lifecycle = _lifecycle(host, port)
    await _create(lifecycle)
    host.notifications.clear()

    result = await lifecycle.revoke(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-revoke",
    )

    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    stored, state = host.records[(identity.project_subject, identity.control_id)]
    person_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=TARGET,
    )
    _my_card, my_card_state = host.records[(TARGET, person_identity.my_card_id)]
    audit = stored.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert result["ok"] is True
    assert result["removed"] is True
    assert state == CARD_STATE_REVOKED
    assert my_card_state == CARD_STATE_REVOKED
    assert result["project_identity_edge_removed"] is True
    assert audit["action"] == "revoked"
    assert audit["actor_subject"] == ADMIN
    assert audit["request_id"] == "request-revoke"
    assert audit["changes"] == {
        "state": {"before": "active", "after": "revoked"},
    }
    assert port.requests[-1].operation == PROJECT_PERSON_CONTROL_REVOKE
    assert host.notifications == [(TARGET, "project_person_control_revoked")]
    assert await host._load_record(
        identity.control_id,
        grantor_subject=identity.project_subject,
    ) is None


@pytest.mark.asyncio
async def test_a_member_is_refused_revoking_its_own_project_card() -> None:
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    port = _Port(deny_operations=frozenset({PROJECT_PERSON_CONTROL_REVOKE}))

    result = await _lifecycle(host, port).revoke(
        actor_subject=TARGET,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-target-revoke",
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_decided_by_admin",
        "status": 403,
    }
    assert host.forget_calls == []


@pytest.mark.asyncio
async def test_reading_a_person_control_card_says_it_is_read_only_here() -> None:
    """W260: the hub view never edits a person Control Card; a project admin is
    sent to the project's editor, anyone else is told an admin decides it."""

    host = _Host()
    await _create(_lifecycle(host, _Port()))

    admin_view = await _lifecycle(host, _Port()).get(
        actor_subject=TARGET, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-read",
    )
    assert admin_view["ok"] is True
    assert admin_view["viewer"] == {
        "can_edit": False,
        "edit_in_project": True,
        "reason": "project_person_control_edited_in_project",
    }

    member_port = _Port(deny_operations=frozenset({PROJECT_PERSON_CONTROL_UPDATE}))
    member_view = await _lifecycle(host, member_port).get(
        actor_subject=TARGET, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-read",
    )
    assert member_view["ok"] is True
    assert member_view["viewer"] == {
        "can_edit": False,
        "edit_in_project": False,
        "reason": "project_person_control_decided_by_admin",
    }
    # The viewer question is the edit question, and it changes nothing.
    assert [request.operation for request in member_port.requests] == [
        PROJECT_PERSON_CONTROL_READ, PROJECT_PERSON_CONTROL_UPDATE,
    ]
    assert host.update_calls == []


# -- composing the person's Card with the Control Card its project holds (W260) --


async def _real_pair():
    """The Control Card and My Card the lifecycle creates, as production has them."""

    host = _Host()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle, migration=True)
    control_identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    person_identity = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)
    control, _ = host.records[(control_identity.project_subject, control_identity.control_id)]
    my_card, _ = host.records[(TARGET, person_identity.my_card_id)]
    return control.authority, my_card.authority


@pytest.mark.asyncio
async def test_the_lifecycle_s_own_cards_compose_through_the_project_held_path() -> None:
    from connection_hub.delegated_credentials.controls.project_person_composition import (
        compose_with_project_held_control,
        project_held_control,
    )

    control, my_card = await _real_pair()
    assert project_held_control(my_card) is not None
    composed = compose_with_project_held_control(my_card, control)
    assert composed.access_id == my_card.access_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"composition_mode": "or"}, "project_person_control_requires_and"),
        ({"issuer_ref": "work:project:other"}, None),
        ({"identity_scope": "delegate"}, "control_card_identity_scope_mismatch"),
        ({"state": "revoked"}, "control_card_not_active"),
    ],
)
async def test_the_project_held_composition_keeps_the_control_card_guards(change, reason) -> None:
    """Review on app-ecosystem#162: every caller gets the guards ordinary composition has."""

    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
    from connection_hub.delegated_credentials.controls.project_person_composition import (
        compose_with_project_held_control,
    )

    control, my_card = await _real_pair()
    with pytest.raises(ControlCardMismatch) as refused:
        compose_with_project_held_control(my_card, dataclasses.replace(control, **change))
    if reason:
        assert refused.value.reason == reason

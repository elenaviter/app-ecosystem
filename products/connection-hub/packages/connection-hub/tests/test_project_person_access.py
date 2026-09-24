# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_REVOKE,
    ProjectAuthorizationDecision,
)
from connection_hub.delegated_credentials.project_person_access import (
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
    def __init__(self, *, deny_actor: str = "", mismatch: bool = False) -> None:
        self.deny_actor = deny_actor
        self.mismatch = mismatch
        self.requests = []

    async def authorize_project_person_control(self, request):
        self.requests.append(request)
        if request.actor_subject == self.deny_actor:
            return ProjectAuthorizationDecision.deny(
                request,
                reason="project_person_control_admin_required",
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
        identity = ProjectPersonControlIdentity.build(
            project_ref=PROJECT_REF,
            target_subject=TARGET,
        )
        existing, _state = self.records[(identity.project_subject, identity.control_id)]
        candidate = _Record(
            dataclasses.replace(
                existing.authority,
                card_revision=existing.card_revision + 1,
                label=kwargs.get("label") or existing.label,
            )
        )
        transform = kwargs["_record_transform"]
        updated = transform(existing, candidate)
        self.records[(identity.project_subject, identity.control_id)] = (
            updated,
            CARD_STATE_ACTIVE,
        )
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
):
    return await lifecycle.create(
        actor_subject=actor,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id=request_id,
        label="Quickstart member",
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
    assert my_card_state == CARD_STATE_ACTIVE
    assert my_card.grantor_subject == TARGET
    assert my_card.delegate_subject == TARGET
    assert my_card.resource_grants == {}
    assert my_card.resource_operations == {}
    assert my_card.named_service_operations.is_none
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
async def test_target_cannot_widen_its_project_card() -> None:
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    host.update_calls.clear()
    permissive_port = _Port()

    result = await _lifecycle(host, permissive_port).update(
        actor_subject=TARGET,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-target-update",
        label="Wider",
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_target_write_denied",
        "status": 403,
    }
    assert host.update_calls == []
    assert permissive_port.requests == []


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
async def test_target_cannot_revoke_its_project_card() -> None:
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    port = _Port()

    result = await _lifecycle(host, port).revoke(
        actor_subject=TARGET,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        request_id="request-target-revoke",
    )

    assert result == {
        "ok": False,
        "error": "project_person_control_target_write_denied",
        "status": 403,
    }
    assert host.forget_calls == []
    assert port.requests == []

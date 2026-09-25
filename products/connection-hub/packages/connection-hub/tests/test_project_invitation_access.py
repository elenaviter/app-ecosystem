# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE,
    PROJECT_INVITATION_CONTROL_PROPERTY,
    ProjectInvitationControlIdentity,
)
from connection_hub.delegated_credentials.controls.project_person import (
    ProjectPersonControlIdentity,
    bind_project_person_control,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    ProjectPersonCardIdentity,
)
from connection_hub.delegated_credentials.project_invitation_access import (
    PROJECT_INVITATION_BINDING_PROVENANCE,
    ProjectInvitationControlLifecycle,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingEvidence,
    normalized_email_digest,
)


PROJECT_REF = "work:project:quickstart"
INVITATION_REF = "work:invitation:inv-1"
ADMIN = "platform-admin-1"
PERSON = "platform-user-2"
EMAIL = " Person@Example.Test "
RESOURCE = "https://board.example.test/mcp"
GRANT = "work:review"
OPERATION = "review.accept"


@dataclass(frozen=True)
class _Record:
    authority: CardAuthority

    def __getattr__(self, name: str) -> Any:
        return getattr(self.authority, name)

    def to_public_dict(self) -> dict[str, Any]:
        return self.authority.to_dict()


class _Reconciled:
    @staticmethod
    def to_public_dict() -> dict[str, Any]:
        return {"resources": [], "claims": [], "named_service_operations": []}


class _Host:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[_Record, str]] = {}
        self.notifications: list[tuple[str, str]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.fail_persist_id = ""
        self.fail_forget_id = ""
        self.forget_barrier: asyncio.Barrier | None = None
        self._write_lock = asyncio.Lock()

    async def _load_record_any_state(self, access_id, *, grantor_subject):
        return self.records.get((grantor_subject, access_id))

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
        named = NamedServiceSelection.from_stored(
            kwargs.get("named_service_operations"),
            present=kwargs.get("named_service_operations") is not None,
        )
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
            named_service_operations=named,
            named_services=(
                {RESOURCE: {"press": {"label": "Press"}}} if not named.is_none else {}
            ),
            account_scope=kwargs.get("account_scope") or {},
            identity_scope="grantor",
            properties=dict(kwargs.get("properties") or {}),
            reconciled=_Reconciled(),
        )

    async def _persist_record(self, record, *, expected_revision):
        assert expected_revision == 0
        if record.access_id == self.fail_persist_id:
            raise CardCommitFailed("planned_test_failure")
        key = (record.grantor_subject, record.access_id)
        async with self._write_lock:
            if key in self.records:
                raise CardConflict("card_revision_conflict", current_revision=1)
            self.records[key] = (record, CARD_STATE_ACTIVE)

    async def _control_card_public_view(
        self,
        user,
        record,
        *,
        state=CARD_STATE_ACTIVE,
        _delegable_grants=(),
        _platform_admin=False,
    ):
        del user, _delegable_grants, _platform_admin
        return {
            **record.to_public_dict(),
            "state": state,
            "catalog_drift": {},
            "resource_offers": [],
        }

    async def update_access(self, user, **kwargs):
        self.update_calls.append({"user": user, **kwargs})
        key = (user["user_id"], kwargs["access_id"])
        existing, _state = self.records[key]
        candidate = _Record(
            dataclasses.replace(
                existing.authority,
                card_revision=existing.card_revision + 1,
                label=kwargs.get("label") or existing.label,
                resource_grants={
                    resource: tuple(grants)
                    for resource, grants in kwargs.get(
                        "resource_grants",
                        existing.resource_grants,
                    ).items()
                },
                resource_operations={
                    resource: tuple(operations)
                    for resource, operations in kwargs.get(
                        "resource_operations",
                        existing.resource_operations,
                    ).items()
                },
                named_service_operations=NamedServiceSelection.from_stored(
                    kwargs.get(
                        "named_service_operations",
                        existing.named_service_operations.to_stored(),
                    ),
                    present=True,
                ),
                account_scope=kwargs.get("account_scope", existing.account_scope),
                properties=kwargs.get("properties", existing.properties),
            )
        )
        updated = kwargs["_record_transform"](existing, candidate)
        self.records[key] = (updated, CARD_STATE_ACTIVE)
        return {"ok": True, "access": updated.to_public_dict(), "pruned": {}}

    async def _forget_record(self, record, *, revoked_record):
        if record.access_id == self.fail_forget_id:
            raise CardCommitFailed("planned_test_failure")
        if self.forget_barrier is not None:
            await self.forget_barrier.wait()
        key = (record.grantor_subject, record.access_id)
        async with self._write_lock:
            current = self.records.get(key)
            if current is None or current[0] is not record:
                revision = current[0].card_revision if current is not None else 0
                raise CardConflict(
                    "card_revision_conflict",
                    current_revision=revision,
                )
            self.records[key] = (revoked_record, revoked_record.state)

    async def notify_change(self, subject, *, action, access=None, access_id=""):
        del access, access_id
        self.notifications.append((subject, action))


class _Port:
    def __init__(self) -> None:
        self.requests = []

    async def authorize_project_person_control(self, request):
        self.requests.append(request)
        return ProjectAuthorizationDecision.allow(
            request,
            delegable_grants=(GRANT,),
            platform_admin=True,
        )


class _BindingResolver:
    def __init__(self, *, email: str = EMAIL, person: str = PERSON) -> None:
        self.email = email
        self.person = person
        self.calls = []

    async def resolve_project_invitation_binding(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
    ):
        self.calls.append((project_ref, invitation_ref))
        identity = ProjectInvitationControlIdentity.build(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            target_email=EMAIL,
        )
        return ProjectInvitationBindingEvidence.build(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=identity.control_id,
            person_subject=self.person,
            email=self.email,
        )


def _lifecycle(
    host: _Host,
    *,
    port: Any | None = None,
    resolver: Any | None = None,
) -> ProjectInvitationControlLifecycle:
    return ProjectInvitationControlLifecycle(
        host=host,
        authorization_port=port if port is not None else _Port(),
        binding_resolver=resolver if resolver is not None else _BindingResolver(),
        authority_from_record=lambda record: record.authority,
        record_from_authority=_Record,
    )


async def _create(lifecycle: ProjectInvitationControlLifecycle):
    return await lifecycle.create(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email=EMAIL,
        request_id="request-create",
        resource_grants={RESOURCE: [GRANT]},
        resource_operations={RESOURCE: [OPERATION]},
        named_service_operations={RESOURCE: {"press": [OPERATION]}},
        account_scope={"slack": {"account-1": ["post"]}},
        label="Invited project member",
    )


@pytest.mark.asyncio
async def test_pending_invitation_card_has_no_person_identity_or_raw_email() -> None:
    host = _Host()
    port = _Port()
    lifecycle = _lifecycle(host, port=port)

    result = await _create(lifecycle)

    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email=EMAIL,
    )
    stored, state = host.records[(identity.project_subject, identity.control_id)]
    assert result["ok"] is True
    assert result["created"] is True
    assert state == CARD_STATE_ACTIVE
    assert stored.properties[PROJECT_INVITATION_CONTROL_PROPERTY] == (
        identity.to_property()
    )
    assert stored.resource_grants == {RESOURCE: (GRANT,)}
    assert stored.resource_operations == {RESOURCE: (OPERATION,)}
    assert stored.named_service_operations.to_stored() == {
        RESOURCE: {"press": [OPERATION]}
    }
    assert stored.account_scope == {"slack": {"account-1": ("post",)}}
    assert EMAIL.strip().lower() not in repr(stored.to_public_dict()).lower()
    assert len(host.records) == 1
    assert port.requests[0].target_subject == INVITATION_REF


@pytest.mark.asyncio
async def test_pending_create_is_idempotent_for_the_same_email_and_conflicts_for_another() -> (
    None
):
    host = _Host()
    lifecycle = _lifecycle(host)
    await _create(lifecycle)

    replay = await _create(lifecycle)
    conflict = await lifecycle.create(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email="other@example.test",
        request_id="request-conflict",
    )

    assert replay["ok"] is True
    assert replay["created"] is False
    assert conflict == {
        "ok": False,
        "error": "project_invitation_control_email_conflict",
        "status": 409,
    }


@pytest.mark.asyncio
async def test_pending_selection_is_editable_and_audited_by_project_admin() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]

    result = await lifecycle.update(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-update",
        label="Reviewed invitation access",
        expected_card_revision=1,
    )

    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email=EMAIL,
    )
    stored, state = host.records[(identity.project_subject, pending_id)]
    audit = stored.provenance[PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE]
    assert result["ok"] is True
    assert state == CARD_STATE_ACTIVE
    assert stored.card_revision == 2
    assert stored.label == "Reviewed invitation access"
    assert audit["action"] == "updated"
    assert audit["actor_subject"] == ADMIN
    assert audit["request_id"] == "request-update"
    assert audit["changes"]["label"] == {
        "before": "Invited project member",
        "after": "Reviewed invitation access",
    }


@pytest.mark.asyncio
async def test_revoked_pending_invitation_cannot_be_bound() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]

    revoked = await lifecycle.revoke(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-revoke",
    )
    bound = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )

    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email=EMAIL,
    )
    stored, state = host.records[(identity.project_subject, pending_id)]
    audit = stored.provenance[PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE]
    assert revoked["ok"] is True
    assert revoked["removed"] is True
    assert state == CARD_STATE_REVOKED
    assert audit["action"] == "revoked"
    assert bound == {
        "ok": False,
        "error": "project_invitation_control_not_active",
        "status": 409,
    }


@pytest.mark.asyncio
async def test_bind_copies_the_invited_selection_to_live_control_and_my_card() -> None:
    host = _Host()
    resolver = _BindingResolver()
    lifecycle = _lifecycle(host, resolver=resolver)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]

    result = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )

    live_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=PERSON,
    )
    my_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=PERSON,
    )
    live, live_state = host.records[
        (live_identity.project_subject, live_identity.control_id)
    ]
    my_card, my_state = host.records[(PERSON, my_identity.my_card_id)]
    pending, pending_state = host.records[(live_identity.project_subject, pending_id)]
    assert result["ok"] is True
    assert result["bound"] is True
    assert result["pending_control_id"] == pending_id
    assert result["control_card"]["access_id"] == live_identity.control_id
    assert live_state == CARD_STATE_ACTIVE
    assert my_state == CARD_STATE_ACTIVE
    assert pending_state == CARD_STATE_REVOKED
    assert live.resource_grants == my_card.resource_grants == {RESOURCE: (GRANT,)}
    assert (
        live.resource_operations
        == my_card.resource_operations
        == {RESOURCE: (OPERATION,)}
    )
    assert live.named_service_operations == my_card.named_service_operations
    assert live.account_scope == my_card.account_scope
    marker = live.provenance[PROJECT_INVITATION_BINDING_PROVENANCE]
    assert marker == my_card.provenance[PROJECT_INVITATION_BINDING_PROVENANCE]
    assert marker == pending.provenance[PROJECT_INVITATION_BINDING_PROVENANCE]
    assert marker["target_email_digest"] == normalized_email_digest(EMAIL)
    assert marker["pending_control_id"] == pending_id
    assert marker["control_id"] == live_identity.control_id


@pytest.mark.asyncio
async def test_bind_replay_preserves_the_persons_later_narrowing() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]
    first = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )
    my_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=PERSON,
    )
    my_key = (PERSON, my_identity.my_card_id)
    my_card, _state = host.records[my_key]
    narrowed = _Record(
        dataclasses.replace(
            my_card.authority,
            card_revision=my_card.card_revision + 1,
            operations=(),
            resource_grants={},
            resource_operations={},
            named_service_operations=NamedServiceSelection.none(),
            named_services={},
            account_scope={},
        )
    )
    host.records[my_key] = (narrowed, CARD_STATE_ACTIVE)

    replay = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind-retry",
    )

    assert first["bound"] is True
    assert replay["ok"] is True
    assert replay["bound"] is False
    assert host.records[my_key][0].resource_grants == {}
    assert host.records[my_key][0].card_revision == 2


@pytest.mark.asyncio
async def test_bind_repairs_a_failure_after_live_control_creation() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]
    my_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=PERSON,
    )
    host.fail_persist_id = my_identity.my_card_id

    failed = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )
    host.fail_persist_id = ""
    repaired = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind-retry",
    )

    assert failed["error"] == "project_invitation_binding_not_committed"
    assert repaired["ok"] is True
    assert repaired["bound"] is False
    assert host.records[(PERSON, my_identity.my_card_id)][1] == CARD_STATE_ACTIVE
    pending_identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        target_email=EMAIL,
    )
    assert (
        host.records[(pending_identity.project_subject, pending_identity.control_id)][1]
        == CARD_STATE_REVOKED
    )


@pytest.mark.asyncio
async def test_a_failed_claim_creates_no_person_authority_and_retry_repairs() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]
    host.fail_forget_id = pending_id

    failed = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )
    live_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=PERSON,
    )
    my_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=PERSON,
    )
    assert failed["error"] == "project_invitation_binding_not_committed"
    assert (
        live_identity.project_subject,
        live_identity.control_id,
    ) not in host.records
    assert (PERSON, my_identity.my_card_id) not in host.records

    host.fail_forget_id = ""
    repaired = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind-retry",
    )

    assert repaired["ok"] is True
    assert repaired["bound"] is True
    assert host.records[(live_identity.project_subject, live_identity.control_id)][1] == (
        CARD_STATE_ACTIVE
    )
    assert host.records[(PERSON, my_identity.my_card_id)][1] == CARD_STATE_ACTIVE
    assert host.records[(live_identity.project_subject, pending_id)][1] == (
        CARD_STATE_REVOKED
    )


@pytest.mark.asyncio
async def test_two_concurrent_binders_leave_authority_only_for_the_claim_winner() -> None:
    host = _Host()
    created = await _create(_lifecycle(host))
    pending_id = created["control_card"]["access_id"]
    other_person = "platform-user-3"
    first = _lifecycle(host, resolver=_BindingResolver(person=PERSON))
    second = _lifecycle(host, resolver=_BindingResolver(person=other_person))
    host.forget_barrier = asyncio.Barrier(2)

    results = await asyncio.gather(
        first.bind(
            actor_subject=PERSON,
            project_ref=PROJECT_REF,
            invitation_ref=INVITATION_REF,
            control_id=pending_id,
            request_id="request-bind-first",
        ),
        second.bind(
            actor_subject=other_person,
            project_ref=PROJECT_REF,
            invitation_ref=INVITATION_REF,
            control_id=pending_id,
            request_id="request-bind-second",
        ),
    )

    applied = [result for result in results if result.get("ok") is True]
    refused = [result for result in results if result.get("ok") is not True]
    assert len(applied) == len(refused) == 1
    assert refused[0]["error"] == "project_invitation_binding_claim_conflict"
    winner = applied[0]["binding"]["person_subject"]
    loser = other_person if winner == PERSON else PERSON
    winner_control = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=winner,
    )
    winner_my = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=winner,
    )
    loser_control = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=loser,
    )
    loser_my = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=loser,
    )
    assert (winner_control.project_subject, winner_control.control_id) in host.records
    assert (winner, winner_my.my_card_id) in host.records
    assert (loser_control.project_subject, loser_control.control_id) not in host.records
    assert (loser, loser_my.my_card_id) not in host.records


@pytest.mark.asyncio
async def test_revoke_winning_the_claim_race_leaves_no_person_authority() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]
    revoked = asyncio.Event()
    original_forget = host._forget_record

    async def revoke_first(record, *, revoked_record):
        marker = revoked_record.provenance.get(
            PROJECT_INVITATION_BINDING_PROVENANCE
        )
        if marker is not None:
            await revoked.wait()
        await original_forget(record, revoked_record=revoked_record)
        if marker is None:
            revoked.set()

    host._forget_record = revoke_first
    bind_result, revoke_result = await asyncio.gather(
        lifecycle.bind(
            actor_subject=PERSON,
            project_ref=PROJECT_REF,
            invitation_ref=INVITATION_REF,
            control_id=pending_id,
            request_id="request-bind-race",
        ),
        lifecycle.revoke(
            actor_subject=ADMIN,
            project_ref=PROJECT_REF,
            invitation_ref=INVITATION_REF,
            control_id=pending_id,
            request_id="request-revoke-race",
        ),
    )

    live_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=PERSON,
    )
    my_identity = ProjectPersonCardIdentity.build(
        project_ref=PROJECT_REF,
        person_subject=PERSON,
    )
    assert revoke_result["ok"] is True
    assert bind_result["error"] == "project_invitation_binding_claim_conflict"
    assert (live_identity.project_subject, live_identity.control_id) not in host.records
    assert (PERSON, my_identity.my_card_id) not in host.records


@pytest.mark.asyncio
async def test_bind_refuses_an_unrelated_existing_live_control_card() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    pending_id = created["control_card"]["access_id"]
    live_identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=PERSON,
    )
    pending = host.records[(live_identity.project_subject, pending_id)][0]
    unrelated = _Record(
        bind_project_person_control(
            dataclasses.replace(
                pending.authority,
                access_id=live_identity.control_id,
                issuer_ref=live_identity.project_ref,
                issuer_kind="project-person",
                properties={},
                provenance={},
            ),
            identity=live_identity,
        )
    )
    host.records[(live_identity.project_subject, live_identity.control_id)] = (
        unrelated,
        CARD_STATE_ACTIVE,
    )

    result = await lifecycle.bind(
        actor_subject=PERSON,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=pending_id,
        request_id="request-bind",
    )

    assert result == {
        "ok": False,
        "error": "project_invitation_binding_live_control_conflict",
        "status": 409,
    }
    assert len(host.records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "email", "error"),
    [
        ("someone-else", EMAIL, "project_invitation_binding_invalid"),
        (PERSON, "other@example.test", "project_invitation_binding_email_mismatch"),
    ],
)
async def test_bind_refuses_mismatched_session_or_email(
    actor: str,
    email: str,
    error: str,
) -> None:
    host = _Host()
    lifecycle = _lifecycle(host, resolver=_BindingResolver(email=email, person=PERSON))
    created = await _create(lifecycle)

    result = await lifecycle.bind(
        actor_subject=actor,
        project_ref=PROJECT_REF,
        invitation_ref=INVITATION_REF,
        control_id=created["control_card"]["access_id"],
        request_id="request-bind",
    )

    assert result["error"] == error
    assert len(host.records) == 1

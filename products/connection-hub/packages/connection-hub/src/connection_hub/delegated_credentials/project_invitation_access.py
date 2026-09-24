# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Pending invitation Control Cards and their one-way person binding."""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any, Callable, Iterable, Mapping

from connection_hub.agent_account_scope import normalize_account_scope
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CONTROL_COMPOSITION_AND,
    CardAuthority,
    CardRecordError,
)
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
    CardServingUnavailable,
    replace_state,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    next_resource_acceptance,
)
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE,
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    PROJECT_INVITATION_CONTROL_PROPERTY,
    ProjectInvitationControlAudit,
    ProjectInvitationControlError,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
    project_invitation_control_id,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlAudit,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
    bind_project_person_control,
    project_authority_subject,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    materialize_control_snapshot,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_INVITATION_CONTROL_CREATE,
    PROJECT_INVITATION_CONTROL_READ,
    PROJECT_INVITATION_CONTROL_REVOKE,
    PROJECT_INVITATION_CONTROL_UPDATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationPort,
    ProjectAuthorizationRequest,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    ProjectIdentityLifecycle,
    ProjectIdentityLifecycleError,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    PROJECT_INVITATION_BINDING_SCHEMA,
    ProjectInvitationBindingError,
    ProjectInvitationBindingEvidence,
    ProjectInvitationBindingResolver,
)


AuthorityFromRecord = Callable[[Any], CardAuthority]
RecordFromAuthority = Callable[[CardAuthority], Any]

PROJECT_INVITATION_BINDING_PROVENANCE = "project_invitation_binding"


def _serving_state_unavailable(exc: CardServingUnavailable) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "delegated_card_serving_state_unavailable",
        "reason": exc.reason,
        "access_id": exc.access_id,
        "retryable": True,
        "status": 503,
    }


class ProjectInvitationControlLifecycle:
    """Admin-managed pending Cards that become one person's live Card."""

    def __init__(
        self,
        *,
        host: Any,
        authorization_port: ProjectAuthorizationPort | None,
        binding_resolver: ProjectInvitationBindingResolver | None,
        authority_from_record: AuthorityFromRecord,
        record_from_authority: RecordFromAuthority,
    ) -> None:
        self._host = host
        self._authorization_port = authorization_port
        self._binding_resolver = binding_resolver
        self._authority_from_record = authority_from_record
        self._record_from_authority = record_from_authority
        self._project_identities = ProjectIdentityLifecycle(
            host=host,
            authority_from_record=authority_from_record,
            record_from_authority=record_from_authority,
        )

    @staticmethod
    def _project_user(project_ref: str) -> dict[str, Any]:
        return {
            "user_id": project_authority_subject(project_ref),
            "roles": [],
            "permissions": [],
        }

    async def _authorize(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        operation: str,
        request_id: str,
    ) -> (
        tuple[ProjectAuthorizationRequest, ProjectAuthorizationDecision]
        | dict[str, Any]
    ):
        try:
            request = ProjectAuthorizationRequest.build(
                actor_subject=actor_subject,
                project_ref=project_ref,
                target_subject=invitation_ref,
                operation=operation,
                request_id=request_id,
            )
        except ProjectAuthorizationError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        if self._authorization_port is None:
            return {
                "ok": False,
                "error": "project_invitation_control_authorization_unavailable",
                "reason": "authorization_port_not_configured",
                "retryable": True,
                "status": 503,
            }
        try:
            decision = await self._authorization_port.authorize_project_person_control(
                request
            )
        except Exception:  # noqa: BLE001 - project policy is an availability boundary
            return {
                "ok": False,
                "error": "project_invitation_control_authorization_unavailable",
                "reason": "authorization_port_failed",
                "retryable": True,
                "status": 503,
            }
        if not isinstance(decision, ProjectAuthorizationDecision):
            return {
                "ok": False,
                "error": "project_invitation_control_authorization_invalid",
                "reason": "authorization_decision_type_invalid",
                "retryable": True,
                "status": 503,
            }
        try:
            decision.validate_for(request)
        except ProjectAuthorizationError as exc:
            return {
                "ok": False,
                "error": "project_invitation_control_authorization_invalid",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if not decision.allowed:
            return {"ok": False, "error": decision.reason, "status": 403}
        return request, decision

    async def _load(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
        control_id: str = "",
    ) -> tuple[Any, str, ProjectInvitationControlIdentity] | dict[str, Any]:
        expected_id = project_invitation_control_id(project_ref, invitation_ref)
        if control_id and str(control_id).strip() != expected_id:
            return {
                "ok": False,
                "error": "project_invitation_control_id_mismatch",
                "status": 409,
            }
        project_subject = project_authority_subject(project_ref)
        try:
            loaded = await self._host._load_record_any_state(
                expected_id,
                grantor_subject=project_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "project_invitation_control_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if loaded is None:
            return {
                "ok": False,
                "error": "project_invitation_control_not_found",
                "status": 404,
            }
        record, state = loaded
        try:
            identity = ProjectInvitationControlIdentity.from_authority(
                self._authority_from_record(record)
            )
        except ProjectInvitationControlError as exc:
            return {
                "ok": False,
                "error": "project_invitation_control_identity_conflict",
                "reason": exc.reason,
                "status": 409,
            }
        if (
            identity.project_ref != str(project_ref).strip()
            or identity.invitation_ref != str(invitation_ref).strip()
        ):
            return {
                "ok": False,
                "error": "project_invitation_control_identity_conflict",
                "status": 409,
            }
        return record, state, identity

    async def _view(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        decision: ProjectAuthorizationDecision,
    ) -> dict[str, Any]:
        loaded = await self._load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(loaded, dict):
            return loaded
        record, state, identity = loaded
        if state == CARD_STATE_ACTIVE:
            try:
                record = await self._host._ensure_control_snapshot(record)
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "project_invitation_control_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
        authority = dataclasses.replace(
            self._authority_from_record(record),
            state=state,
        )
        card = await self._host._control_card_public_view(
            self._project_user(project_ref),
            record,
            state=state,
            _delegable_grants=decision.delegable_grants,
            _platform_admin=decision.platform_admin,
        )
        access = record.to_public_dict()
        access["state"] = state
        access["catalog_drift"] = dict(card.get("catalog_drift") or {})
        access["resource_offers"] = list(card.get("resource_offers") or [])
        provenance = dict(authority.provenance or {})
        return {
            "ok": True,
            "control_card": card,
            "card": card,
            "access": access,
            "authority": authority.to_dict(),
            "project_invitation_control": identity.to_property(),
            "audit": copy.deepcopy(
                provenance.get(PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE, {})
            ),
            "binding": copy.deepcopy(
                provenance.get(PROJECT_INVITATION_BINDING_PROVENANCE, {})
            ),
        }

    async def get(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            operation=PROJECT_INVITATION_CONTROL_READ,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        _request, decision = authorized
        return await self._view(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
            decision=decision,
        )

    async def create(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        target_email: str,
        request_id: str,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        composition_mode: str = CONTROL_COMPOSITION_AND,
        label: str = "",
        manage_url: str = "",
    ) -> dict[str, Any]:
        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            operation=PROJECT_INVITATION_CONTROL_CREATE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, decision = authorized
        if str(composition_mode or "").strip().lower() != CONTROL_COMPOSITION_AND:
            return {
                "ok": False,
                "error": "project_invitation_control_requires_and",
                "status": 400,
            }
        try:
            identity = ProjectInvitationControlIdentity.build(
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                target_email=target_email,
            )
        except (ProjectInvitationBindingError, ProjectInvitationControlError) as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        existing = await self._load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=identity.control_id,
        )
        if not isinstance(existing, dict):
            _record, state, stored_identity = existing
            if stored_identity.target_email_digest != identity.target_email_digest:
                return {
                    "ok": False,
                    "error": "project_invitation_control_email_conflict",
                    "status": 409,
                }
            if state != CARD_STATE_ACTIVE:
                return {
                    "ok": False,
                    "error": "project_invitation_control_not_active",
                    "status": 409,
                }
            result = await self._view(
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                control_id=identity.control_id,
                decision=decision,
            )
            if result.get("ok") is True:
                result["created"] = False
            return result
        if existing.get("error") != "project_invitation_control_not_found":
            return existing

        try:
            active = await self._host._active_catalog()
            catalog_version = self._host._version_of(active)
            catalog_config = await self._host._catalog_config(
                active,
                owner_subject=identity.project_subject,
            )
            authority = bind_project_invitation_control(
                new_credentialless_card(
                    control_id=identity.control_id,
                    grantor_subject=identity.project_subject,
                    catalog_version=catalog_version,
                    issuer_ref=identity.invitation_ref,
                    issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND,
                    issuer_label=label or "Pending project invitation",
                    manage_url=manage_url,
                    properties=properties,
                    composition_mode=CONTROL_COMPOSITION_AND,
                    now=int(time.time()),
                ),
                identity=identity,
            )
            record = self._record_from_authority(authority)
            pruned: dict[str, Any] = {
                "resources": [],
                "claims": [],
                "named_service_operations": [],
            }
            if any(
                selection is not None
                for selection in (
                    resource_grants,
                    resource_operations,
                    named_service_operations,
                    account_scope,
                )
            ):
                resolved = await self._host._resolve_card_authority(
                    user=self._project_user(project_ref),
                    existing=record,
                    active=active,
                    resource_grants=resource_grants,
                    resource_operations=resource_operations,
                    operations=(),
                    named_service_operations=named_service_operations,
                    account_scope=account_scope,
                    properties=record.properties,
                    _delegable_grants=decision.delegable_grants,
                    _platform_admin=decision.platform_admin,
                )
                if resolved.error is not None:
                    return resolved.error
                if resolved.revoke:
                    return {
                        "ok": False,
                        "error": "project_invitation_control_selection_empty",
                        "status": 409,
                        "pruned": resolved.reconciled.to_public_dict(),
                    }
                pruned = resolved.reconciled.to_public_dict()
                record = self._record_from_authority(
                    dataclasses.replace(
                        self._authority_from_record(record),
                        operations=tuple(resolved.operations),
                        resource_grants={
                            key: tuple(value)
                            for key, value in resolved.resource_grants.items()
                        },
                        resource_operations={
                            key: tuple(value)
                            for key, value in resolved.resource_operations.items()
                        },
                        named_service_operations=resolved.named_service_operations,
                        named_services=copy.deepcopy(resolved.named_services),
                        account_scope={
                            provider: {
                                account_id: tuple(claims)
                                for account_id, claims in accounts.items()
                            }
                            for provider, accounts in resolved.account_scope.items()
                        },
                        identity_scope=resolved.identity_scope,
                        properties=resolved.properties,
                        resource_acceptance=next_resource_acceptance(
                            resources=resolved.resource_grants,
                            row_for=lambda resource: self._host._configured_resource(
                                resource,
                                config=catalog_config,
                            ),
                            catalog_version=catalog_version,
                            selected_operations=resolved.resource_operations,
                        ),
                    )
                )
            authority = bind_project_invitation_control(
                materialize_control_snapshot(
                    self._authority_from_record(record),
                    basis_catalog_version=catalog_version,
                    origin="created",
                ),
                identity=identity,
            )
            audit = ProjectInvitationControlAudit.build(
                action="created",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=None,
                after=authority,
            )
            record = self._record_from_authority(
                bind_project_invitation_control(
                    authority,
                    identity=identity,
                    audit=audit,
                )
            )
            await self._host._persist_record(record, expected_revision=0)
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        except (
            CardRecordError,
            ControlCardError,
            ProjectInvitationControlError,
        ) as exc:
            return {
                "ok": False,
                "error": getattr(exc, "reason", str(exc)),
                "status": 400,
            }
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "project_invitation_control_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self._host.notify_change(
            request.actor_subject,
            action="project_invitation_control_created",
            access=record.to_public_dict(),
        )
        result = await self._view(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=identity.control_id,
            decision=decision,
        )
        if result.get("ok") is True:
            result["created"] = True
            result["pruned"] = pruned
        return result

    async def update(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        composition_mode: str | None = None,
        label: str | None = None,
        expected_card_revision: int | None = None,
        expected_catalog_version: str | None = None,
        accepted_operations: Mapping[str, Iterable[str]] | None = None,
    ) -> dict[str, Any]:
        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            operation=PROJECT_INVITATION_CONTROL_UPDATE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, decision = authorized
        if (
            composition_mode is not None
            and str(composition_mode).strip().lower() != CONTROL_COMPOSITION_AND
        ):
            return {
                "ok": False,
                "error": "project_invitation_control_requires_and",
                "status": 400,
            }
        loaded = await self._load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(loaded, dict):
            return loaded
        existing, state, identity = loaded
        if state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }
        before = self._authority_from_record(existing)

        def _stamp_audit(_before: Any, candidate: Any) -> Any:
            del _before
            authority = bind_project_invitation_control(
                self._authority_from_record(candidate),
                identity=identity,
            )
            audit = ProjectInvitationControlAudit.build(
                action="updated",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=before,
                after=authority,
            )
            return self._record_from_authority(
                bind_project_invitation_control(
                    authority,
                    identity=identity,
                    audit=audit,
                )
            )

        try:
            updated = await self._host.update_access(
                self._project_user(project_ref),
                access_id=identity.control_id,
                resource_grants=(
                    resource_grants
                    if resource_grants is not None
                    else existing.resource_grants
                ),
                resource_operations=(
                    resource_operations
                    if resource_operations is not None
                    else existing.resource_operations
                ),
                named_service_operations=(
                    named_service_operations
                    if named_service_operations is not None
                    else existing.named_service_operations.to_stored()
                ),
                account_scope=(
                    account_scope
                    if account_scope is not None
                    else existing.account_scope
                ),
                properties=(
                    properties if properties is not None else existing.properties
                ),
                composition_mode=CONTROL_COMPOSITION_AND,
                label=label,
                expected_card_revision=expected_card_revision,
                expected_catalog_version=expected_catalog_version,
                accepted_operations=accepted_operations,
                _delegable_grants=decision.delegable_grants,
                _platform_admin=decision.platform_admin,
                _record_transform=_stamp_audit,
                _notification_subject=request.actor_subject,
            )
        except ProjectInvitationControlError as exc:
            status = (
                409
                if exc.reason == "project_invitation_control_audit_changes_empty"
                else 400
            )
            return {"ok": False, "error": exc.reason, "status": status}
        if updated.get("ok") is not True:
            return updated
        result = await self._view(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=identity.control_id,
            decision=decision,
        )
        if updated.get("pruned") is not None:
            result["pruned"] = updated["pruned"]
        return result

    async def revoke(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            operation=PROJECT_INVITATION_CONTROL_REVOKE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, _decision = authorized
        loaded = await self._load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(loaded, dict):
            if loaded.get("error") == "project_invitation_control_not_found":
                return {"ok": True, "removed": False}
            return loaded
        existing, state, identity = loaded
        before = self._authority_from_record(existing)
        binding = dict(before.provenance or {}).get(
            PROJECT_INVITATION_BINDING_PROVENANCE
        )
        if binding is not None:
            return {
                "ok": False,
                "error": "project_invitation_control_already_bound",
                "status": 409,
            }
        if state == CARD_STATE_REVOKED:
            return {
                "ok": True,
                "removed": False,
                "control_id": identity.control_id,
                "project_invitation_control": identity.to_property(),
            }
        if state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }
        try:
            revoked = bind_project_invitation_control(
                replace_state(before, CARD_STATE_REVOKED),
                identity=identity,
            )
            audit = ProjectInvitationControlAudit.build(
                action="revoked",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=before,
                after=revoked,
            )
            revoked_record = self._record_from_authority(
                bind_project_invitation_control(
                    revoked,
                    identity=identity,
                    audit=audit,
                )
            )
            await self._host._forget_record(existing, revoked_record=revoked_record)
        except ProjectInvitationControlError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "project_invitation_control_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self._host.notify_change(
            request.actor_subject,
            action="project_invitation_control_revoked",
            access_id=identity.control_id,
        )
        return {
            "ok": True,
            "removed": True,
            "control_id": identity.control_id,
            "project_invitation_control": identity.to_property(),
            "audit": audit.to_dict(),
        }

    @staticmethod
    def _binding_marker(
        *,
        evidence: ProjectInvitationBindingEvidence,
        target_email_digest: str,
        live_control_id: str,
        request_id: str,
        bound_at: int,
    ) -> dict[str, Any]:
        return {
            "schema": PROJECT_INVITATION_BINDING_SCHEMA,
            "project_ref": evidence.project_ref,
            "invitation_ref": evidence.invitation_ref,
            "pending_control_id": evidence.control_id,
            "control_id": live_control_id,
            "person_subject": evidence.person_subject,
            "target_email_digest": target_email_digest,
            "request_id": request_id,
            "bound_at": bound_at,
        }

    @staticmethod
    def _validate_binding_marker(
        marker: Any,
        *,
        evidence: ProjectInvitationBindingEvidence,
        target_email_digest: str,
        live_control_id: str,
    ) -> dict[str, Any]:
        if not isinstance(marker, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        expected = {
            "schema": PROJECT_INVITATION_BINDING_SCHEMA,
            "project_ref": evidence.project_ref,
            "invitation_ref": evidence.invitation_ref,
            "pending_control_id": evidence.control_id,
            "control_id": live_control_id,
            "person_subject": evidence.person_subject,
            "target_email_digest": target_email_digest,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_mismatch"
            )
        if not str(marker.get("request_id") or "").strip():
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        if type(marker.get("bound_at")) is not int or marker["bound_at"] < 1:
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        return copy.deepcopy(dict(marker))

    async def _binding_evidence(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
    ) -> ProjectInvitationBindingEvidence | dict[str, Any]:
        if self._binding_resolver is None:
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": "binding_resolver_not_configured",
                "retryable": True,
                "status": 503,
            }
        try:
            evidence = await self._binding_resolver.resolve_project_invitation_binding(
                project_ref=project_ref,
                invitation_ref=invitation_ref,
            )
        except ProjectInvitationBindingError as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        except Exception:  # noqa: BLE001 - provider is an availability boundary
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": "binding_resolver_failed",
                "retryable": True,
                "status": 503,
            }
        if evidence is None:
            return {
                "ok": False,
                "error": "project_invitation_binding_not_found",
                "status": 403,
            }
        if not isinstance(evidence, ProjectInvitationBindingEvidence):
            return {
                "ok": False,
                "error": "project_invitation_binding_invalid",
                "reason": "binding_evidence_type_invalid",
                "status": 503,
                "retryable": True,
            }
        try:
            evidence.validate_for(
                project_ref=str(project_ref).strip(),
                invitation_ref=str(invitation_ref).strip(),
                control_id=str(control_id).strip(),
                person_subject=str(actor_subject).strip(),
            )
        except ProjectInvitationBindingError as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_invalid",
                "reason": exc.reason,
                "status": 403,
            }
        return evidence

    async def bind(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Consume one pending Card into the joining person's stable Card."""

        actor = str(actor_subject or "").strip()
        if not actor:
            return {
                "ok": False,
                "error": "project_invitation_binding_person_subject_missing",
                "status": 400,
            }
        evidence = await self._binding_evidence(
            actor_subject=actor,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(evidence, dict):
            return evidence
        loaded = await self._load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(loaded, dict):
            return loaded
        pending_record, pending_state, pending_identity = loaded
        if pending_identity.target_email_digest != evidence.email_digest:
            return {
                "ok": False,
                "error": "project_invitation_binding_email_mismatch",
                "status": 403,
            }
        pending = dataclasses.replace(
            self._authority_from_record(pending_record),
            state=pending_state,
        )
        live_identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=actor,
        )
        pending_marker = dict(pending.provenance or {}).get(
            PROJECT_INVITATION_BINDING_PROVENANCE
        )
        project_subject = live_identity.project_subject
        try:
            loaded_live = await self._host._load_record_any_state(
                live_identity.control_id,
                grantor_subject=project_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "project_person_control_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        live_record = None
        live_state = ""
        live_marker: Any = None
        if loaded_live is not None:
            live_record, live_state = loaded_live
            try:
                stored_live_identity = ProjectPersonControlIdentity.from_authority(
                    self._authority_from_record(live_record)
                )
            except ProjectPersonControlError as exc:
                return {
                    "ok": False,
                    "error": "project_person_control_identity_conflict",
                    "reason": exc.reason,
                    "status": 409,
                }
            if stored_live_identity != live_identity or live_state != CARD_STATE_ACTIVE:
                return {
                    "ok": False,
                    "error": "project_invitation_binding_live_control_conflict",
                    "status": 409,
                }
            live_marker = dict(
                self._authority_from_record(live_record).provenance or {}
            ).get(PROJECT_INVITATION_BINDING_PROVENANCE)
            if live_marker is None:
                return {
                    "ok": False,
                    "error": "project_invitation_binding_live_control_conflict",
                    "status": 409,
                }
        if pending_state == CARD_STATE_REVOKED and pending_marker is None:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }
        if pending_state not in {CARD_STATE_ACTIVE, CARD_STATE_REVOKED}:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }

        marker_source = pending_marker if pending_marker is not None else live_marker
        try:
            marker = (
                self._validate_binding_marker(
                    marker_source,
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                )
                if marker_source is not None
                else self._binding_marker(
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                    request_id=str(request_id or "").strip(),
                    bound_at=int(time.time()),
                )
            )
            if live_marker is not None:
                self._validate_binding_marker(
                    live_marker,
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                )
        except ProjectInvitationBindingError as exc:
            return {"ok": False, "error": exc.reason, "status": 409}
        if not marker.get("request_id"):
            return {
                "ok": False,
                "error": "project_invitation_binding_request_id_missing",
                "status": 400,
            }

        created = False
        try:
            if live_record is None:
                if pending_state != CARD_STATE_ACTIVE:
                    return {
                        "ok": False,
                        "error": "project_invitation_binding_incomplete",
                        "status": 409,
                    }
                public_properties = copy.deepcopy(dict(pending.properties or {}))
                public_properties.pop(PROJECT_INVITATION_CONTROL_PROPERTY, None)
                live = bind_project_person_control(
                    new_credentialless_card(
                        control_id=live_identity.control_id,
                        grantor_subject=live_identity.project_subject,
                        catalog_version=pending.catalog_version,
                        initial_selection=pending,
                        issuer_ref=live_identity.project_ref,
                        issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
                        issuer_label=pending.issuer_label or pending.label,
                        manage_url=pending.manage_url,
                        properties=public_properties,
                        composition_mode=CONTROL_COMPOSITION_AND,
                        now=int(time.time()),
                    ),
                    identity=live_identity,
                )
                audit = ProjectPersonControlAudit.build(
                    action="bound_from_invitation",
                    actor_subject=actor,
                    identity=live_identity,
                    request_id=marker["request_id"],
                    occurred_at=marker["bound_at"],
                    before=None,
                    after=live,
                )
                provenance = copy.deepcopy(dict(live.provenance or {}))
                provenance[PROJECT_INVITATION_BINDING_PROVENANCE] = marker
                live = bind_project_person_control(
                    dataclasses.replace(live, provenance=provenance),
                    identity=live_identity,
                    audit=audit,
                )
                live_record = self._record_from_authority(live)
                await self._host._persist_record(live_record, expected_revision=0)
                created = True
            live = self._authority_from_record(live_record)
            identity_result = await self._project_identities.ensure(
                live_record,
                initial_selection=live,
                initial_provenance={
                    PROJECT_INVITATION_BINDING_PROVENANCE: marker,
                },
            )
            if pending_state == CARD_STATE_ACTIVE:
                revoked = bind_project_invitation_control(
                    replace_state(pending, CARD_STATE_REVOKED),
                    identity=pending_identity,
                )
                pending_provenance = copy.deepcopy(dict(revoked.provenance or {}))
                pending_provenance[PROJECT_INVITATION_BINDING_PROVENANCE] = marker
                revoked = bind_project_invitation_control(
                    dataclasses.replace(revoked, provenance=pending_provenance),
                    identity=pending_identity,
                )
                pending_audit = ProjectInvitationControlAudit.build(
                    action="bound",
                    actor_subject=actor,
                    identity=pending_identity,
                    request_id=marker["request_id"],
                    occurred_at=marker["bound_at"],
                    before=pending,
                    after=revoked,
                )
                revoked_record = self._record_from_authority(
                    bind_project_invitation_control(
                        revoked,
                        identity=pending_identity,
                        audit=pending_audit,
                    )
                )
                await self._host._forget_record(
                    pending_record,
                    revoked_record=revoked_record,
                )
        except (
            CardRecordError,
            ControlCardError,
            ProjectIdentityLifecycleError,
            ProjectInvitationControlError,
            ProjectPersonControlError,
        ) as exc:
            return {
                "ok": False,
                "error": getattr(exc, "reason", str(exc)),
                "status": 409,
            }
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }

        if created:
            await self._host.notify_change(
                actor,
                action="project_invitation_control_bound",
                access=live_record.to_public_dict(),
            )
        return {
            "ok": True,
            "bound": created,
            "pending_control_id": pending_identity.control_id,
            "control_card": live_record.to_public_dict(),
            "my_card": identity_result.my_card.to_public_dict(),
            "project_identity_edge": identity_result.edge.to_dict(),
            "binding": copy.deepcopy(marker),
        }


__all__ = [
    "PROJECT_INVITATION_BINDING_PROVENANCE",
    "ProjectInvitationControlLifecycle",
]

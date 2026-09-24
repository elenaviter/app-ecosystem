# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Lifecycle service for project-held, per-person Control Cards."""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any, Callable, Iterable, Mapping

from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CONTROL_COMPOSITION_AND,
    CardAuthority,
    CardRecordError,
)
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
    CardServingUnavailable,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    next_resource_acceptance,
)
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlAudit,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
    bind_project_person_control,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    materialize_control_snapshot,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_READ,
    PROJECT_PERSON_CONTROL_UPDATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationPort,
    ProjectAuthorizationRequest,
)


AuthorityFromRecord = Callable[[Any], CardAuthority]
RecordFromAuthority = Callable[[CardAuthority], Any]


def _serving_state_unavailable(exc: CardServingUnavailable) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "delegated_card_serving_state_unavailable",
        "reason": exc.reason,
        "access_id": exc.access_id,
        "retryable": True,
        "status": 503,
    }


class ProjectPersonControlLifecycle:
    """Project-authorized lifecycle over the ordinary durable Card store."""

    def __init__(
        self,
        *,
        host: Any,
        authorization_port: ProjectAuthorizationPort | None,
        authority_from_record: AuthorityFromRecord,
        record_from_authority: RecordFromAuthority,
    ) -> None:
        self._host = host
        self._authorization_port = authorization_port
        self._authority_from_record = authority_from_record
        self._record_from_authority = record_from_authority

    async def _authorize(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        operation: str,
        request_id: str,
    ) -> tuple[ProjectAuthorizationRequest, ProjectAuthorizationDecision] | dict[str, Any]:
        if (
            operation == PROJECT_PERSON_CONTROL_UPDATE
            and str(actor_subject or "").strip() == str(target_subject or "").strip()
        ):
            return {
                "ok": False,
                "error": "project_person_control_target_write_denied",
                "status": 403,
            }
        try:
            request = ProjectAuthorizationRequest.build(
                actor_subject=actor_subject,
                project_ref=project_ref,
                target_subject=target_subject,
                operation=operation,
                request_id=request_id,
            )
        except ProjectAuthorizationError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        if self._authorization_port is None:
            return {
                "ok": False,
                "error": "project_person_control_authorization_unavailable",
                "reason": "authorization_port_not_configured",
                "retryable": True,
                "status": 503,
            }
        try:
            decision = await self._authorization_port.authorize_project_person_control(
                request
            )
        except Exception:  # noqa: BLE001 - the policy host is an availability boundary
            return {
                "ok": False,
                "error": "project_person_control_authorization_unavailable",
                "reason": "authorization_port_failed",
                "retryable": True,
                "status": 503,
            }
        if not isinstance(decision, ProjectAuthorizationDecision):
            return {
                "ok": False,
                "error": "project_person_control_authorization_invalid",
                "reason": "authorization_decision_type_invalid",
                "retryable": True,
                "status": 503,
            }
        try:
            decision.validate_for(request)
        except ProjectAuthorizationError as exc:
            return {
                "ok": False,
                "error": "project_person_control_authorization_invalid",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if not decision.allowed:
            return {
                "ok": False,
                "error": decision.reason,
                "status": 403,
            }
        return request, decision

    @staticmethod
    def _project_user(identity: ProjectPersonControlIdentity) -> dict[str, Any]:
        """Storage identity only; authorization remains in the injected port."""

        return {
            "user_id": identity.project_subject,
            "roles": [],
            "permissions": [],
        }

    async def _load(
        self,
        identity: ProjectPersonControlIdentity,
    ) -> tuple[Any, str] | dict[str, Any]:
        try:
            loaded = await self._host._load_record_any_state(
                identity.control_id,
                grantor_subject=identity.project_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "project_person_control_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if loaded is None:
            return {
                "ok": False,
                "error": "project_person_control_not_found",
                "status": 404,
            }
        record, state = loaded
        try:
            ProjectPersonControlIdentity.from_authority(
                self._authority_from_record(record)
            )
        except ProjectPersonControlError as exc:
            return {
                "ok": False,
                "error": "project_person_control_identity_conflict",
                "reason": exc.reason,
                "status": 409,
            }
        return record, state

    async def _view(
        self,
        *,
        identity: ProjectPersonControlIdentity,
        decision: ProjectAuthorizationDecision,
    ) -> dict[str, Any]:
        loaded = await self._load(identity)
        if isinstance(loaded, dict):
            return loaded
        record, state = loaded
        if state == CARD_STATE_ACTIVE:
            try:
                record = await self._host._ensure_control_snapshot(record)
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "project_person_control_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
        authority = dataclasses.replace(
            self._authority_from_record(record),
            state=state,
        )
        card = await self._host._control_card_public_view(
            self._project_user(identity),
            record,
            state=state,
            _delegable_grants=decision.delegable_grants,
            _platform_admin=decision.platform_admin,
        )
        access = record.to_public_dict()
        access["state"] = state
        access["catalog_drift"] = dict(card.get("catalog_drift") or {})
        access["resource_offers"] = list(card.get("resource_offers") or [])
        return {
            "ok": True,
            "control_card": card,
            "card": card,
            "access": access,
            "authority": authority.to_dict(),
            "project_person_control": identity.to_property(),
            "audit": copy.deepcopy(
                dict(authority.provenance or {}).get(
                    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
                    {},
                )
            ),
        }

    async def get(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            operation=PROJECT_PERSON_CONTROL_READ,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        _request, decision = authorized
        identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=target_subject,
        )
        return await self._view(identity=identity, decision=decision)

    async def create(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
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
            target_subject=target_subject,
            operation=PROJECT_PERSON_CONTROL_CREATE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, decision = authorized
        identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=target_subject,
        )
        existing = await self._load(identity)
        if not isinstance(existing, dict):
            _record, state = existing
            if state != CARD_STATE_ACTIVE:
                return {
                    "ok": False,
                    "error": "project_person_control_not_active",
                    "status": 409,
                }
            result = await self._view(identity=identity, decision=decision)
            if result.get("ok") is True:
                result["created"] = False
            return result
        if existing.get("error") != "project_person_control_not_found":
            return existing

        try:
            active = await self._host._active_catalog()
            catalog_version = self._host._version_of(active)
            catalog_config = await self._host._catalog_config(
                active,
                owner_subject=identity.project_subject,
            )
            authority = bind_project_person_control(
                new_credentialless_card(
                    control_id=identity.control_id,
                    grantor_subject=identity.project_subject,
                    catalog_version=catalog_version,
                    issuer_ref=identity.project_ref,
                    issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
                    issuer_label=label or target_subject,
                    manage_url=manage_url,
                    properties=properties,
                    composition_mode=composition_mode,
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
                    user=self._project_user(identity),
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
                        "error": "project_person_control_selection_empty",
                        "status": 409,
                        "pruned": resolved.reconciled.to_public_dict(),
                    }
                pruned = resolved.reconciled.to_public_dict()
                record = dataclasses.replace(
                    record,
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
            authority = bind_project_person_control(
                materialize_control_snapshot(
                    self._authority_from_record(record),
                    basis_catalog_version=catalog_version,
                    origin="created",
                ),
                identity=identity,
            )
            audit = ProjectPersonControlAudit.build(
                action="created",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=None,
                after=authority,
            )
            record = self._record_from_authority(
                bind_project_person_control(
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
        except (CardRecordError, ControlCardError, ProjectPersonControlError) as exc:
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
                "error": "project_person_control_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self._host.notify_change(
            identity.target_subject,
            action="project_person_control_created",
            access=record.to_public_dict(),
        )
        result = await self._view(identity=identity, decision=decision)
        if result.get("ok") is True:
            result["created"] = True
            result["pruned"] = pruned
        return result

    async def update(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
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
            target_subject=target_subject,
            operation=PROJECT_PERSON_CONTROL_UPDATE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, decision = authorized
        identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=target_subject,
        )
        loaded = await self._load(identity)
        if isinstance(loaded, dict):
            return loaded
        existing, state = loaded
        if state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_person_control_not_active",
                "status": 409,
            }
        before = self._authority_from_record(existing)

        def _stamp_audit(_before: Any, candidate: Any) -> Any:
            del _before
            authority = bind_project_person_control(
                self._authority_from_record(candidate),
                identity=identity,
            )
            audit = ProjectPersonControlAudit.build(
                action="updated",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=before,
                after=authority,
            )
            return self._record_from_authority(
                bind_project_person_control(
                    authority,
                    identity=identity,
                    audit=audit,
                )
            )

        try:
            updated = await self._host.update_access(
                self._project_user(identity),
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
                composition_mode=(
                    composition_mode
                    if composition_mode is not None
                    else existing.composition_mode
                ),
                label=label,
                expected_card_revision=expected_card_revision,
                expected_catalog_version=expected_catalog_version,
                accepted_operations=accepted_operations,
                _delegable_grants=decision.delegable_grants,
                _platform_admin=decision.platform_admin,
                _record_transform=_stamp_audit,
                _notification_subject=identity.target_subject,
            )
        except ProjectPersonControlError as exc:
            status = 409 if exc.reason == "project_person_control_audit_changes_empty" else 400
            return {"ok": False, "error": exc.reason, "status": status}
        if updated.get("ok") is not True:
            return updated
        result = await self._view(identity=identity, decision=decision)
        if updated.get("pruned") is not None:
            result["pruned"] = updated["pruned"]
        return result


__all__ = [
    "ProjectPersonControlLifecycle",
]

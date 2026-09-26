# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Lifecycle service for project-held, per-person Control Cards."""

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
    NamedServiceSelection,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
    CardServingUnavailable,
    replace_state,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    canonical_digest,
    next_resource_acceptance,
)
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    intersect_card_authority_selection,
)
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
    control_snapshot_is_exact,
    materialize_control_snapshot,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_READ,
    PROJECT_PERSON_CONTROL_REVOKE,
    PROJECT_PERSON_CONTROL_UPDATE,
    PROJECT_PERSON_MY_CARD_SEED,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationPort,
    ProjectAuthorizationRequest,
)
from connection_hub.delegated_credentials.project_identity_authorization import (
    ProjectOperationAuthorizationDecision,
    ProjectOperationRequest,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    PROJECT_PERSON_MY_CARD_ISSUER_KIND,
    ProjectIdentityLifecycle,
    ProjectIdentityLifecycleError,
    ProjectIdentityLifecycleResult,
    ProjectPersonCardIdentity,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_grants,
    normalize_resource_operations,
)


AuthorityFromRecord = Callable[[Any], CardAuthority]
RecordFromAuthority = Callable[[CardAuthority], Any]

PROJECT_PERSON_MY_CARD_SEED_PROVENANCE = "project_person_my_card_seed"
PROJECT_PERSON_MY_CARD_SEED_SCHEMA = "connection_hub.project_person_my_card_seed.v1"
PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE = "project_person_control_migration"
PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA = (
    "connection_hub.project_person_control_migration.v1"
)
PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE = (
    "project_person_control_project_creation"
)
PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA = (
    "connection_hub.project_person_control_project_creation.v1"
)


def _seed_request(
    *,
    project_ref: str,
    target_subject: str,
    resource_grants: Mapping[str, Any],
    resource_operations: Mapping[str, Any],
    named_service_operations: Mapping[str, Any] | str,
    account_scope: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    normalized = {
        "project_ref": str(project_ref or "").strip(),
        "target_subject": str(target_subject or "").strip(),
        "resource_grants": normalize_resource_grants(resource_grants),
        "resource_operations": normalize_resource_operations(resource_operations),
        "named_service_operations": NamedServiceSelection.from_stored(
            named_service_operations,
            present=True,
        ).to_stored(),
        "account_scope": normalize_account_scope(account_scope),
    }
    return canonical_digest(normalized), normalized


def _my_card_untouched(authority: CardAuthority) -> bool:
    return bool(
        authority.card_revision == 1
        and not authority.operations
        and not authority.resource_grants
        and not authority.resource_operations
        and authority.named_service_operations.is_none
        and not authority.named_services
        and not authority.account_scope
        and not authority.resource_acceptance
    )


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
        self._project_identities = ProjectIdentityLifecycle(
            host=host,
            authority_from_record=authority_from_record,
            record_from_authority=record_from_authority,
        )

    @staticmethod
    def _identity_failure(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, ProjectIdentityLifecycleError):
            return {"ok": False, "error": exc.reason, "status": 409}
        if isinstance(exc, CardServingUnavailable):
            return _serving_state_unavailable(exc)
        if isinstance(exc, CardUnavailable):
            return {
                "ok": False,
                "error": "project_identity_edge_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if isinstance(exc, (CardConflict, CardCommitFailed)):
            return {
                "ok": False,
                "error": "project_identity_edge_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        raise exc

    @staticmethod
    def _identity_view(
        result: dict[str, Any],
        identity: ProjectIdentityLifecycleResult,
    ) -> dict[str, Any]:
        result["project_identity_edge"] = identity.edge.to_dict()
        result["my_card"] = identity.my_card.to_public_dict()
        result["my_card_created"] = identity.my_card_created
        return result

    async def _authorize(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        operation: str,
        request_id: str,
    ) -> tuple[ProjectAuthorizationRequest, ProjectAuthorizationDecision] | dict[str, Any]:
        # Changing one's own project Control Card is decided by the policy
        # port like any other change: a project admin may (their own included),
        # anyone else is refused there (W260, the operator's rule of
        # 2026-09-26).
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
        view = await self._view(identity=identity, decision=decision)
        if view.get("ok") is True:
            view["viewer"] = await self._viewer(
                actor_subject=actor_subject,
                project_ref=project_ref,
                target_subject=target_subject,
                request_id=request_id,
            )
        return view

    async def _viewer(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        """What this viewer may do with the Card they are reading (W260).

        A person's Control Card is changed only in the project's own editor
        (the board's Team > People), which keeps the board's decision the one
        source of truth; here it is read-only for everyone. A project admin
        is told to edit it there, anyone else that an admin decides it. The
        policy port answers, with the same question an edit would ask.
        """

        admin = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            operation=PROJECT_PERSON_CONTROL_UPDATE,
            request_id=f"{request_id}:viewer",
        )
        edits_in_project = not isinstance(admin, dict)
        return {
            "can_edit": False,
            "edit_in_project": edits_in_project,
            "reason": (
                "project_person_control_edited_in_project"
                if edits_in_project
                else "project_person_control_decided_by_admin"
            ),
        }

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
        migration: bool = False,
        project_creation: bool = False,
    ) -> dict[str, Any]:
        if migration and project_creation:
            return {
                "ok": False,
                "error": "project_person_control_seed_origin_conflict",
                "status": 400,
            }
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
        selected_composition_mode = (
            str(composition_mode or "").strip().lower() or CONTROL_COMPOSITION_AND
        )
        if selected_composition_mode != CONTROL_COMPOSITION_AND:
            return {
                "ok": False,
                "error": "project_person_control_requires_and",
                "status": 400,
            }
        identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=target_subject,
        )
        existing = await self._load(identity)
        if not isinstance(existing, dict):
            record, state = existing
            if state != CARD_STATE_ACTIVE:
                return {
                    "ok": False,
                    "error": "project_person_control_not_active",
                    "status": 409,
                }
            try:
                project_identity = await self._project_identities.ensure(record)
            except (
                CardUnavailable,
                CardServingUnavailable,
                CardConflict,
                CardCommitFailed,
                ProjectIdentityLifecycleError,
            ) as exc:
                return self._identity_failure(exc)
            result = await self._view(identity=identity, decision=decision)
            if result.get("ok") is True:
                result["created"] = False
                self._identity_view(result, project_identity)
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
                    composition_mode=selected_composition_mode,
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
            seed_origin = (
                (
                    PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE,
                    PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA,
                )
                if migration
                else (
                    PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE,
                    PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA,
                )
                if project_creation
                else None
            )
            if seed_origin is not None:
                provenance_key, provenance_schema = seed_origin
                origin_marker = {
                    "schema": provenance_schema,
                    "actor_subject": request.actor_subject,
                    "request_id": request.request_id,
                    "created_at": audit.occurred_at,
                }
                provenance = copy.deepcopy(dict(record.provenance or {}))
                provenance[provenance_key] = origin_marker
                record = self._record_from_authority(
                    dataclasses.replace(
                        self._authority_from_record(record),
                        provenance=provenance,
                    )
                )
            await self._host._persist_record(record, expected_revision=0)
            project_identity = await self._project_identities.ensure(record)
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
            ProjectIdentityLifecycleError,
            ProjectPersonControlError,
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
            self._identity_view(result, project_identity)
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
        if (
            composition_mode is not None
            and str(composition_mode).strip().lower() != CONTROL_COMPOSITION_AND
        ):
            return {
                "ok": False,
                "error": "project_person_control_requires_and",
                "status": 400,
            }
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
                composition_mode=CONTROL_COMPOSITION_AND,
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

    async def revoke(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Revoke one project-held Card through the durable Card lifecycle."""

        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            operation=PROJECT_PERSON_CONTROL_REVOKE,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, _decision = authorized
        identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=target_subject,
        )
        try:
            edge_removed = await self._project_identities.end(
                project_ref=project_ref,
                person_subject=target_subject,
            )
        except (
            CardUnavailable,
            CardServingUnavailable,
            CardConflict,
            CardCommitFailed,
            ProjectIdentityLifecycleError,
        ) as exc:
            return self._identity_failure(exc)
        loaded = await self._load(identity)
        if isinstance(loaded, dict):
            if loaded.get("error") == "project_person_control_not_found":
                return {
                    "ok": True,
                    "removed": False,
                    "project_identity_edge_removed": edge_removed,
                }
            return loaded
        existing, state = loaded
        before = self._authority_from_record(existing)
        if state == CARD_STATE_REVOKED:
            return {
                "ok": True,
                "removed": False,
                "control_id": identity.control_id,
                "project_identity_edge_removed": edge_removed,
                "project_person_control": identity.to_property(),
                "audit": copy.deepcopy(
                    dict(before.provenance or {}).get(
                        PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
                        {},
                    )
                ),
            }
        if state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_person_control_not_active",
                "status": 409,
            }
        try:
            revoked = bind_project_person_control(
                replace_state(before, CARD_STATE_REVOKED),
                identity=identity,
            )
            audit = ProjectPersonControlAudit.build(
                action="revoked",
                actor_subject=request.actor_subject,
                identity=identity,
                request_id=request.request_id,
                occurred_at=int(time.time()),
                before=before,
                after=revoked,
            )
            revoked_record = self._record_from_authority(
                bind_project_person_control(
                    revoked,
                    identity=identity,
                    audit=audit,
                )
            )
            await self._host._forget_record(
                existing,
                revoked_record=revoked_record,
            )
        except ProjectPersonControlError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
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
            action="project_person_control_revoked",
            access_id=identity.control_id,
        )
        return {
            "ok": True,
            "removed": True,
            "control_id": identity.control_id,
            "project_identity_edge_removed": edge_removed,
            "project_person_control": identity.to_property(),
            "audit": audit.to_dict(),
        }

    async def seed_my_card(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        target_subject: str,
        request_id: str,
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any],
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Seed one untouched My Card during the project-person migration."""

        authorized = await self._authorize(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            operation=PROJECT_PERSON_MY_CARD_SEED,
            request_id=request_id,
        )
        if isinstance(authorized, dict):
            return authorized
        request, decision = authorized
        try:
            request_digest, normalized = _seed_request(
                project_ref=project_ref,
                target_subject=target_subject,
                resource_grants=resource_grants,
                resource_operations=resource_operations,
                named_service_operations=named_service_operations or {},
                account_scope=account_scope or {},
            )
        except (CardRecordError, ValueError) as exc:
            return {
                "ok": False,
                "error": getattr(exc, "reason", "project_person_my_card_seed_invalid"),
                "status": 400,
            }
        try:
            resolution = await self._project_identities.resolve(
                project_ref=project_ref,
                person_subject=target_subject,
            )
        except (
            CardUnavailable,
            CardServingUnavailable,
            CardConflict,
            CardCommitFailed,
            ProjectIdentityLifecycleError,
        ) as exc:
            return self._identity_failure(exc)

        control = resolution.control_card.authority
        my_card = resolution.my_card.authority
        if resolution.edge is None or control is None or my_card is None:
            return {
                "ok": False,
                "error": "project_identity_edge_missing",
                "status": 409,
            }
        if control.state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_person_control_not_active",
                "status": 409,
            }
        if (
            not authority_is_credentialless(control)
            or control.composition_mode != CONTROL_COMPOSITION_AND
            or not control_snapshot_is_exact(control)
        ):
            return {
                "ok": False,
                "error": "project_person_control_invalid",
                "status": 409,
            }
        provenance = dict(control.provenance or {})
        seed_origins = [
            ("migration", provenance.get(PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE)),
            (
                "project_creation",
                provenance.get(PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE),
            ),
        ]
        present_origins = [entry for entry in seed_origins if entry[1] is not None]
        if not present_origins:
            return {
                "ok": False,
                "error": "project_person_my_card_seed_not_migrated",
                "status": 409,
            }
        if len(present_origins) != 1:
            return {
                "ok": False,
                "error": "project_person_control_seed_origin_conflict",
                "status": 409,
            }
        seed_origin, origin_marker = present_origins[0]
        expected_origin_schema = {
            "migration": PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA,
            "project_creation": PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA,
        }[seed_origin]
        if (
            not isinstance(origin_marker, Mapping)
            or origin_marker.get("schema") != expected_origin_schema
        ):
            return {
                "ok": False,
                "error": f"project_person_control_{seed_origin}_marker_invalid",
                "status": 409,
            }
        if my_card.state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_identity_my_card_not_active",
                "status": 409,
            }
        if my_card.issuer_kind != PROJECT_PERSON_MY_CARD_ISSUER_KIND:
            return {
                "ok": False,
                "error": "project_identity_my_card_issuer_mismatch",
                "status": 409,
            }

        marker = dict(my_card.provenance or {}).get(
            PROJECT_PERSON_MY_CARD_SEED_PROVENANCE
        )
        if marker is not None:
            if (
                not isinstance(marker, Mapping)
                or marker.get("schema") != PROJECT_PERSON_MY_CARD_SEED_SCHEMA
            ):
                return {
                    "ok": False,
                    "error": "project_person_my_card_seed_marker_invalid",
                    "status": 409,
                }
            if marker.get("request_digest") != request_digest:
                return {
                    "ok": False,
                    "error": "project_person_my_card_seed_conflict",
                    "status": 409,
                }
            return {
                "ok": True,
                "seeded": False,
                "my_card": self._record_from_authority(my_card).to_public_dict(),
                "authority": my_card.to_dict(),
                "project_identity_edge": resolution.edge.to_dict(),
                "seed": copy.deepcopy(dict(marker)),
            }
        if not _my_card_untouched(my_card):
            return {
                "ok": False,
                "error": "project_person_my_card_already_managed",
                "status": 409,
            }

        seed_marker = {
            "schema": PROJECT_PERSON_MY_CARD_SEED_SCHEMA,
            "project_ref": project_ref,
            "target_subject": target_subject,
            "actor_subject": request.actor_subject,
            "request_id": request.request_id,
            "request_digest": request_digest,
            "origin": seed_origin,
            "control_id": control.access_id,
            "control_revision": control.card_revision,
            "seeded_at": int(time.time()),
        }

        def _cap_and_stamp(_before: Any, candidate: Any) -> Any:
            candidate_authority = self._authority_from_record(candidate)
            capped = intersect_card_authority_selection(
                candidate_authority,
                control,
            )
            provenance = copy.deepcopy(dict(candidate_authority.provenance or {}))
            provenance[PROJECT_PERSON_MY_CARD_SEED_PROVENANCE] = seed_marker
            return self._record_from_authority(
                dataclasses.replace(
                    candidate_authority,
                    operations=capped.operations,
                    resource_grants=capped.resource_grants,
                    resource_operations=capped.resource_operations,
                    named_service_operations=capped.named_service_operations,
                    named_services=capped.named_services,
                    account_scope=capped.account_scope,
                    resource_acceptance={
                        resource: acceptance
                        for resource, acceptance in (
                            candidate_authority.resource_acceptance.items()
                        )
                        if resource in capped.resource_grants
                    },
                    provenance=provenance,
                )
            )

        identity = ProjectPersonCardIdentity.build(
            project_ref=project_ref,
            person_subject=target_subject,
        )
        try:
            updated = await self._host.update_access(
                {"user_id": target_subject, "roles": [], "permissions": []},
                access_id=identity.my_card_id,
                resource_grants=normalized["resource_grants"],
                resource_operations=normalized["resource_operations"],
                named_service_operations=normalized["named_service_operations"],
                account_scope=normalized["account_scope"],
                expected_card_revision=1,
                properties=my_card.properties,
                _delegable_grants=decision.delegable_grants,
                _platform_admin=decision.platform_admin,
                _record_transform=_cap_and_stamp,
                _notification_subject=target_subject,
            )
        except ControlCardMismatch as exc:
            return {"ok": False, "error": exc.reason, "status": 409}
        if updated.get("ok") is not True:
            return updated
        try:
            current = await self._project_identities.resolve(
                project_ref=project_ref,
                person_subject=target_subject,
            )
        except (
            CardUnavailable,
            CardServingUnavailable,
            CardConflict,
            CardCommitFailed,
            ProjectIdentityLifecycleError,
        ) as exc:
            return self._identity_failure(exc)
        current_my_card = current.my_card.authority
        if current.edge is None or current_my_card is None:
            return {
                "ok": False,
                "error": "project_identity_edge_missing",
                "status": 409,
            }
        result = {
            "ok": True,
            "seeded": True,
            "my_card": self._record_from_authority(
                current_my_card
            ).to_public_dict(),
            "authority": current_my_card.to_dict(),
            "project_identity_edge": current.edge.to_dict(),
            "seed": copy.deepcopy(seed_marker),
        }
        if updated.get("pruned") is not None:
            result["pruned"] = updated["pruned"]
        return result

    async def authorize_operation(
        self,
        request: ProjectOperationRequest,
    ) -> ProjectOperationAuthorizationDecision:
        """Resolve the live project edge and evaluate one exact operation."""

        return await self._project_identities.authorize(request)


__all__ = [
    "PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE",
    "PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA",
    "PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE",
    "PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA",
    "PROJECT_PERSON_MY_CARD_SEED_PROVENANCE",
    "PROJECT_PERSON_MY_CARD_SEED_SCHEMA",
    "ProjectPersonControlLifecycle",
]

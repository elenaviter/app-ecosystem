# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Synchronize descriptor-owned Control Cards for resident KDCube agents."""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any, Iterable, Mapping

from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_ISSUER_KIND,
    descriptor_acceptance,
    descriptor_control_properties,
    resident_selection_properties,
)
from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_PROJECTION_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AgentCapabilityPolicy,
    AgentCapabilityPolicyError,
    capability_states,
    replace_visible_selection,
)
from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_API_RESOURCE,
    ApplicationOperationPolicyError,
    application_operation_role_policy,
    validate_application_operation_role_policy,
)
from connection_hub.delegated_credentials.application_resources import (
    application_resource,
)
from connection_hub.delegated_credentials.automation_access import (
    ACCESS_SOURCE_AGENT,
    ACCESS_SOURCE_CONTROL,
    AutomationAccessRecord,
    ResolvedCardAuthority,
    _application_policy_refusal,
    _clean,
    _delegate_mutation_refusal,
    _record_is_credentialless,
    _selection_policy_argument,
    _serving_state_unavailable,
    _subject_from_user,
    card_authority_from_record,
    record_from_card,
)
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AGENT,
    CARD_KIND_CONTROL,
    resident_client_id,
    stable_resident_access_id,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CONTROL_COMPOSITION_AND,
    CardAuthority,
    CardRecordError,
    ControlCardBinding,
    NamedServiceSelection,
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
from connection_hub.delegated_credentials.catalog.reconcile import reconcile_selection
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.model import (
    control_card_id_for_issuer,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    control_snapshot_refusal,
    reviewed_control_snapshot_properties,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    CONVERSATION_TARGETS_PROPERTY,
)
from connection_hub.delegated_credentials.named_service_policy import (
    named_service_policy_for_resource,
)
from connection_hub.delegated_credentials.oauth.grants import integration_subject
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_operations,
    operation_union,
    resolve_declared_resource_keys,
)


# A descriptor-synchronized Agent Card carries capability selection, not a
# bearer. Its expiry is an inactivity lease that bounds stale authority when an
# application stops syncing. One week avoids access-token-scale churn while
# keeping abandoned resident projections finite. The first agent message after
# a lapse renews this same Card id and preserves its selection in a new revision.
AGENT_CAPABILITY_CARD_LEASE_SECONDS = 7 * 24 * 60 * 60


async def resolve_agent_descriptor_standard_authority(
    service: Any,
    *,
    active: Any,
    owner_subject: str,
    resource_grants: Mapping[str, Any],
    resource_operations: Mapping[str, Any] | None,
    named_service_operations: Mapping[str, Any] | str | None,
    properties: Mapping[str, Any],
) -> "ResolvedCardAuthority":
    """Resolve a descriptor ceiling without treating it as a user grant.

    A Control Card only narrows a participant Card. Its exact ceiling may
    therefore name authority the current user does not hold; intersection
    can never contribute that authority. The catalog still validates every
    resource, claim, operation, role, and named-service entry.
    """

    catalog_config = await service._catalog_config(
        active,
        owner_subject=owner_subject,
    )
    selected_grants = service._resource_grants(resource_grants)
    selected_grants, _host_pinned = service._declared_resource_keys(
        catalog_config,
        selected_grants,
    )
    if not selected_grants:
        return ResolvedCardAuthority(
            named_service_operations=NamedServiceSelection.none(),
            properties=copy.deepcopy(dict(properties)),
        )

    try:
        selected_operations = normalize_resource_operations(resource_operations or {})
        selected_operations, _rewritten = resolve_declared_resource_keys(
            catalog_config,
            selected_operations,
        )
        selection = service._named_service_operation_selection(named_service_operations)
        if selection is None or selection.is_unknown:
            selection = NamedServiceSelection.none()
        selection = service._declared_named_service_selection(
            catalog_config,
            selection,
        )
    except (CardRecordError, ValueError) as exc:
        return ResolvedCardAuthority(
            error={
                "ok": False,
                "error": "agent_descriptor_authority_invalid",
                "message": str(exc),
                "status": 400,
            }
        )

    reconciled = reconcile_selection(
        resource_grants=selected_grants,
        resource_operations=selected_operations,
        named_service_operations=selection,
        active=active,
        config=catalog_config,
    )
    selected_grants = reconciled.resource_grants
    selected_operations = reconciled.resource_operations
    selection = reconciled.named_service_operations
    selected_resources = list(selected_grants)
    try:
        resource_pairs = service._configured_resource_pairs(
            selected_resources,
            config=catalog_config,
        )
    except ValueError as exc:
        return ResolvedCardAuthority(
            error={
                "ok": False,
                "error": "agent_descriptor_authority_invalid",
                "message": str(exc),
                "status": 400,
            }
        )

    try:
        if APPLICATION_API_RESOURCE in selected_grants:
            application_policy = application_operation_role_policy(
                properties,
                resource_grants=selected_grants,
            )
            if application_policy is None:
                raise ApplicationOperationPolicyError(
                    "application_operation_policy_required"
                )
            validate_application_operation_role_policy(
                application_policy,
                selected_operations=selected_operations.get(
                    APPLICATION_API_RESOURCE,
                    (),
                ),
                allowed_roles=catalog_config.supported_scopes(APPLICATION_API_RESOURCE),
            )
    except ApplicationOperationPolicyError as exc:
        return ResolvedCardAuthority(error=_application_policy_refusal(exc))

    identity_scopes = {
        _clean(getattr(row, "identity_scope", "") or "grantor")
        for _, row in resource_pairs
    }
    if len(identity_scopes) > 1:
        return ResolvedCardAuthority(
            error={
                "ok": False,
                "error": "agent_descriptor_identity_scope_conflict",
                "identity_scopes": sorted(identity_scopes),
                "status": 400,
            }
        )

    named_services: dict[str, Any] = {}
    for resource, row in resource_pairs:
        row_named_services = getattr(row, "named_services", None)
        if not isinstance(row_named_services, Mapping):
            continue
        try:
            selected_policy = named_service_policy_for_resource(
                named_services=row_named_services,
                resource=resource,
                selection=_selection_policy_argument(selection),
                grants=selected_grants.get(resource, ()),
            )
        except ValueError as exc:
            return ResolvedCardAuthority(
                error={
                    "ok": False,
                    "error": "agent_descriptor_authority_invalid",
                    "message": str(exc),
                    "status": 400,
                }
            )
        named_services = service._merge_named_service_configs(
            named_services,
            selected_policy,
        )

    return ResolvedCardAuthority(
        resource_grants=selected_grants,
        resource_operations=selected_operations,
        operations=list(operation_union(selected_operations)),
        named_service_operations=selection,
        named_services=named_services,
        account_scope={},
        identity_scope=next(iter(identity_scopes), "grantor"),
        properties=copy.deepcopy(dict(properties)),
        reconciled=reconciled,
    )


async def sync_agent_capability_control(
    service: Any,
    user: Mapping[str, Any],
    *,
    application: str,
    agent_id: str,
    descriptor_revision: str,
    descriptor_payload: Mapping[str, Any],
    capability_authority: Mapping[str, Any],
    capability_metadata: Mapping[str, Any] | None = None,
    capability_catalog: Mapping[str, Any] | None = None,
    selected_capabilities: Mapping[str, Any] | None = None,
    replace_selection: bool = False,
    conversation_target_resources: Iterable[str] = (),
    resource_grants: Mapping[str, Any] | None = None,
    resource_operations: Mapping[str, Any] | None = None,
    named_service_operations: Mapping[str, Any] | str | None = None,
    properties: Mapping[str, Any] | None = None,
    issuer_label: str = "",
    manage_url: str = "",
) -> dict[str, Any]:
    """Synchronize one resident agent's live descriptor ceiling.

    The descriptor Control Card and the resident participant Card have
    stable ids. Descriptor changes revise only the former. The latter is
    revised for first attachment or an explicit selection replacement, so
    a newly published capability is offered but never selected implicitly.
    """

    grantor_subject = _subject_from_user(user)
    if not grantor_subject:
        return {
            "ok": False,
            "error": "delegated_access_requires_authenticated_user",
        }
    refusal = _delegate_mutation_refusal(user)
    if refusal is not None:
        return refusal
    if not isinstance(descriptor_payload, Mapping):
        return {
            "ok": False,
            "error": "agent_descriptor_payload_invalid",
            "status": 400,
        }

    try:
        agent_resource = application_resource(
            tenant=service._tenant,
            project=service._project,
            application=application,
            agent=agent_id,
        )
        authority = AgentCapabilityPolicy.from_property(capability_authority)
        if authority.resource != agent_resource:
            raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")
        catalog = (
            AgentCapabilityPolicy.from_property(capability_catalog)
            if capability_catalog is not None
            else authority
        )
        if catalog.resource != agent_resource:
            raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")
        requested_selection = (
            AgentCapabilityPolicy.from_property(selected_capabilities)
            if selected_capabilities is not None
            else None
        )
        if (
            requested_selection is not None
            and requested_selection.resource != agent_resource
        ):
            raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")
        descriptor_properties = descriptor_control_properties(
            authority=authority,
            metadata=capability_metadata,
            targets=conversation_target_resources,
        )
        extra_properties = copy.deepcopy(dict(properties or {}))
        reserved = set(extra_properties) & set(descriptor_properties)
        if reserved:
            raise AgentCapabilityPolicyError("agent_descriptor_reserved_property")
        descriptor_properties = {
            **extra_properties,
            **descriptor_properties,
        }
    except (AgentCapabilityPolicyError, ValueError) as exc:
        return {
            "ok": False,
            "error": getattr(exc, "reason", "agent_descriptor_invalid"),
            "message": str(exc),
            "status": 400,
        }

    try:
        active = await service._active_catalog()
        catalog_version = service._version_of(active)
        resolved = await resolve_agent_descriptor_standard_authority(
            service,
            active=active,
            owner_subject=grantor_subject,
            resource_grants=resource_grants or {},
            resource_operations=resource_operations,
            named_service_operations=named_service_operations,
            properties=descriptor_properties,
        )
    except CatalogUnavailable as exc:
        return {
            "ok": False,
            "error": "delegated_catalog_unavailable",
            "reason": exc.reason,
            "retryable": True,
            "status": 503,
        }
    if resolved.error is not None:
        return resolved.error

    descriptor_evidence_payload = {
        "descriptor": copy.deepcopy(dict(descriptor_payload)),
        "capability_authority": authority.to_property(),
        "capability_metadata": copy.deepcopy(dict(capability_metadata or {})),
        "conversation_targets": list(
            descriptor_properties.get(CONVERSATION_TARGETS_PROPERTY) or []
        ),
        "resource_grants": copy.deepcopy(resolved.resource_grants),
        "resource_operations": copy.deepcopy(resolved.resource_operations),
        "named_service_operations": (resolved.named_service_operations.to_stored()),
    }
    try:
        descriptor_evidence = descriptor_acceptance(
            authority=authority,
            descriptor_revision=descriptor_revision,
            descriptor_payload=descriptor_evidence_payload,
        )
    except AgentCapabilityPolicyError as exc:
        return {"ok": False, "error": exc.reason, "status": 400}

    control_id = control_card_id_for_issuer(
        AGENT_DESCRIPTOR_ISSUER_KIND,
        agent_resource,
        grantor_subject=grantor_subject,
    )
    try:
        loaded_control = await service._load_record_any_state(
            control_id,
            grantor_subject=grantor_subject,
        )
    except CardUnavailable as exc:
        return {
            "ok": False,
            "error": "control_card_unavailable",
            "reason": exc.reason,
            "retryable": True,
            "status": 503,
        }
    existing_control = loaded_control[0] if loaded_control is not None else None
    if loaded_control is not None and loaded_control[1] != CARD_STATE_ACTIVE:
        return {
            "ok": False,
            "error": "agent_descriptor_control_not_active",
            "status": 409,
        }
    if existing_control is not None and (
        not _record_is_credentialless(existing_control)
        or existing_control.issuer_kind != AGENT_DESCRIPTOR_ISSUER_KIND
        or existing_control.issuer_ref != agent_resource
    ):
        return {
            "ok": False,
            "error": "agent_descriptor_control_identity_conflict",
            "status": 409,
        }

    catalog_config = await service._catalog_config(
        active,
        owner_subject=grantor_subject,
    )
    control_acceptance = next_resource_acceptance(
        resources=resolved.resource_grants,
        row_for=lambda resource: service._configured_resource(
            resource,
            config=catalog_config,
        ),
        catalog_version=catalog_version,
        selected_operations=resolved.resource_operations,
        previous=(
            existing_control.resource_acceptance
            if existing_control is not None
            else None
        ),
    )
    control_acceptance[agent_resource] = descriptor_evidence
    control_properties = reviewed_control_snapshot_properties(
        resolved.properties,
        basis_catalog_version=catalog_version,
    )
    now = int(time.time())
    control_revision = (
        existing_control.card_revision + 1 if existing_control is not None else 1
    )
    control_authority = CardAuthority(
        access_id=control_id,
        client_id=f"control-card:{AGENT_DESCRIPTOR_ISSUER_KIND}",
        grantor_subject=grantor_subject,
        delegate_subject="",
        source=ACCESS_SOURCE_CONTROL,
        card_kind=CARD_KIND_CONTROL,
        label=_clean(issuer_label) or f"{application} / {agent_id}",
        card_revision=control_revision,
        catalog_version=catalog_version,
        state=CARD_STATE_ACTIVE,
        resource_grants={
            resource: tuple(grants)
            for resource, grants in resolved.resource_grants.items()
        },
        resource_operations={
            resource: tuple(operations)
            for resource, operations in resolved.resource_operations.items()
        },
        named_service_operations=resolved.named_service_operations,
        named_services=copy.deepcopy(resolved.named_services),
        account_scope={},
        identity_scope=resolved.identity_scope or "grantor",
        created_at=(existing_control.created_at if existing_control else now),
        expires_at=0,
        resource_acceptance=control_acceptance,
        provenance=(
            copy.deepcopy(dict(existing_control.provenance or {}))
            if existing_control is not None
            else {}
        ),
        issuer_ref=agent_resource,
        issuer_kind=AGENT_DESCRIPTOR_ISSUER_KIND,
        issuer_label=_clean(issuer_label),
        manage_url=_clean(manage_url),
        composition_mode=CONTROL_COMPOSITION_AND,
        properties=control_properties,
    )
    snapshot_refusal = control_snapshot_refusal(control_authority)
    if snapshot_refusal is not None:
        return snapshot_refusal

    control_changed = True
    if existing_control is not None:
        comparable = dataclasses.replace(
            control_authority,
            card_revision=existing_control.card_revision,
        )
        control_changed = (
            comparable.to_dict()
            != card_authority_from_record(existing_control).to_dict()
        )
    if control_changed:
        try:
            await service._persist_record(
                record_from_card(control_authority),
                expected_revision=(
                    existing_control.card_revision
                    if existing_control is not None
                    else 0
                ),
            )
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "agent_descriptor_control_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
    else:
        control_authority = card_authority_from_record(existing_control)

    client_id = resident_client_id(application, agent_id)
    resident_id = stable_resident_access_id(grantor_subject, client_id)
    try:
        loaded_resident = await service._load_record_any_state(
            resident_id,
            grantor_subject=grantor_subject,
        )
    except CardUnavailable as exc:
        return {
            "ok": False,
            "error": "delegated_cards_unavailable",
            "reason": exc.reason,
            "retryable": True,
            "status": 503,
        }
    existing_resident = loaded_resident[0] if loaded_resident is not None else None
    if loaded_resident is not None and loaded_resident[1] != CARD_STATE_ACTIVE:
        return {
            "ok": False,
            "error": "agent_capability_card_not_active",
            "status": 409,
        }
    if existing_resident is not None and (
        existing_resident.source != ACCESS_SOURCE_AGENT
        or existing_resident.client_id != client_id
    ):
        return {
            "ok": False,
            "error": "agent_capability_card_identity_conflict",
            "status": 409,
        }
    if (
        existing_resident is not None
        and existing_resident.control_card is not None
        and existing_resident.control_card.control_id != control_id
    ):
        return {
            "ok": False,
            "error": "agent_capability_control_conflict",
            "control_card": existing_resident.control_card.to_dict(),
            "status": 409,
        }

    try:
        raw_current_selection = (
            dict(existing_resident.properties or {}).get(
                AGENT_CAPABILITY_SELECTION_PROPERTY
            )
            if existing_resident is not None
            else None
        )
        current_selection = (
            AgentCapabilityPolicy.from_property(raw_current_selection)
            if raw_current_selection is not None
            else (
                requested_selection
                if requested_selection is not None
                else AgentCapabilityPolicy.empty(agent_resource)
            )
        )
        if current_selection.resource != agent_resource:
            raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")
        if replace_selection:
            if requested_selection is None:
                raise AgentCapabilityPolicyError("agent_capability_selection_missing")
            selected = replace_visible_selection(
                current=current_selection,
                authority=authority,
                requested=requested_selection,
            )
        else:
            selected = current_selection
    except AgentCapabilityPolicyError as exc:
        return {"ok": False, "error": exc.reason, "status": 400}

    binding = (
        existing_resident.control_card
        if existing_resident is not None and existing_resident.control_card is not None
        else ControlCardBinding(
            control_id=control_id,
            issuer_ref=agent_resource,
            issuer_kind=AGENT_DESCRIPTOR_ISSUER_KIND,
            issuer_label=_clean(issuer_label),
            manage_url=_clean(manage_url),
            control_revision=control_authority.card_revision,
        )
    )
    if existing_resident is None:
        resident = AutomationAccessRecord(
            access_id=resident_id,
            label=_clean(issuer_label) or f"{application} / {agent_id}",
            client_id=client_id,
            grantor_subject=grantor_subject,
            delegate_subject=integration_subject(
                grantor_subject,
                client_id=client_id,
            ),
            card_kind=CARD_KIND_AGENT,
            operations=(),
            resource_grants={},
            resource_operations={},
            named_service_operations=NamedServiceSelection.none(),
            named_services={},
            account_scope={},
            identity_scope="grantor",
            catalog_version=catalog_version,
            card_revision=1,
            created_at=now,
            expires_at=now + AGENT_CAPABILITY_CARD_LEASE_SECONDS,
            source=ACCESS_SOURCE_AGENT,
            resource_acceptance={agent_resource: descriptor_evidence},
            control_card=binding,
            properties=resident_selection_properties(
                {},
                selection=selected,
            ),
        )
        expected_resident_revision = 0
    else:
        acceptance = dict(existing_resident.resource_acceptance or {})
        if replace_selection or agent_resource not in acceptance:
            acceptance[agent_resource] = descriptor_evidence
        resident = dataclasses.replace(
            existing_resident,
            card_revision=existing_resident.card_revision + 1,
            catalog_version=(
                catalog_version
                if replace_selection
                else existing_resident.catalog_version
            ),
            expires_at=(
                now + AGENT_CAPABILITY_CARD_LEASE_SECONDS
                if existing_resident.expires_at <= now
                and not existing_resident.access_token
                else existing_resident.expires_at
            ),
            resource_acceptance=acceptance,
            control_card=binding,
            properties=resident_selection_properties(
                existing_resident.properties,
                selection=selected,
            ),
        )
        expected_resident_revision = existing_resident.card_revision

    resident_changed = True
    if existing_resident is not None:
        comparable = dataclasses.replace(
            card_authority_from_record(resident),
            card_revision=existing_resident.card_revision,
        )
        resident_changed = (
            comparable.to_dict()
            != card_authority_from_record(existing_resident).to_dict()
        )
    if resident_changed:
        try:
            await service._persist_record(
                resident,
                expected_revision=expected_resident_revision,
            )
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "agent_capability_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
    else:
        resident = existing_resident

    try:
        effective = effective_card_authority(
            card_authority_from_record(resident),
            control_authority,
        )
        projection = AgentCapabilityPolicy.from_property(
            effective.properties[AGENT_CAPABILITY_PROJECTION_PROPERTY]
        )
    except (ControlCardMismatch, AgentCapabilityPolicyError) as exc:
        return {
            "ok": False,
            "error": getattr(exc, "reason", "agent_capability_projection_failed"),
            "status": 409,
        }

    return {
        "ok": True,
        "resource": agent_resource,
        "card": resident.to_public_dict(),
        "control_card": record_from_card(control_authority).to_public_dict(),
        "authority": authority.to_property(),
        "selection": selected.to_property(),
        "projection": projection.to_property(),
        "states": capability_states(
            catalog=catalog,
            authority=authority,
            selection=selected,
        ),
        "control_changed": control_changed,
        "card_changed": resident_changed,
        "pruned": (
            resolved.reconciled.to_public_dict()
            if resolved.reconciled is not None
            else {
                "resources": [],
                "claims": [],
                "named_service_operations": [],
            }
        ),
    }


__all__ = [
    "AGENT_CAPABILITY_CARD_LEASE_SECONDS",
    "resolve_agent_descriptor_standard_authority",
    "sync_agent_capability_control",
]

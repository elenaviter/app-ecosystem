# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Materialize administrator Control Card presets from KDCube agent descriptors."""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_ACCEPTANCE_KIND,
    AGENT_DESCRIPTOR_ISSUER_KIND,
    descriptor_acceptance,
    descriptor_control_properties,
    preserve_descriptor_acceptance,
    resident_selection_properties,
)
from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_DEFAULTS_PROPERTY,
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
    is_resident_client_id,
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
    configured_named_service_operations,
    named_service_policy_for_resource,
    operation_grants,
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


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return []
    return sorted({str(item or "").strip() for item in values if str(item or "").strip()})


def _capability_children(
    policy: AgentCapabilityPolicy | None,
    category: str,
    parent: str,
) -> set[str]:
    if policy is None:
        return set()
    prefix = f"{parent}/"
    return {
        unquote(value[len(prefix) :])
        for value in policy.capabilities.get(category, ())
        if value.startswith(prefix)
    }


def _operation_matches(declared: str, operation: str) -> bool:
    return declared == "*" or operation == declared or operation.startswith(
        f"{declared}."
    )


_NAMED_SERVICE_OUTER_OPERATION = {
    "provider.about": "named_services_about",
    "provider.capabilities": "named_services_capabilities",
    "object.list": "named_services_list",
    "object.search": "named_services_search",
    "object.get": "named_services_get",
    "object.schema": "named_services_schema",
    "object.upsert": "named_services_upsert",
    "object.host_file": "named_services_host_file",
    "object.action": "named_services_action",
    "object.delete": "named_services_delete",
}


def _named_service_outer_operations(
    inner_operations: Iterable[str],
    offered_operations: Iterable[str],
) -> set[str]:
    """Map selected inner operations to their exact MCP bridge operations."""

    offered = {
        str(operation or "").strip()
        for operation in offered_operations
        if str(operation or "").strip()
    }
    selected: set[str] = set()
    for operation in sorted({
        str(value or "").strip()
        for value in inner_operations
        if str(value or "").strip()
    }):
        family = "object.action" if operation.startswith("object.action.") else operation
        bridge = _NAMED_SERVICE_OUTER_OPERATION.get(family)
        if bridge in offered:
            selected.add(bridge)
        elif "named_services_call" in offered:
            # Some providers expose only the generic bridge. The inner Card
            # boundary still carries the exact operation it may invoke.
            selected.add("named_services_call")
    return selected


def _named_operation_grants(
    named_services: Mapping[str, Any],
    namespace: str,
) -> dict[str, set[str]]:
    namespaces = named_services.get("namespaces")
    if not isinstance(namespaces, Mapping):
        return {}
    raw_namespace = next(
        (
            value
            for name, value in namespaces.items()
            if str(name or "").strip().lower().rstrip(":") == namespace
            and isinstance(value, Mapping)
        ),
        None,
    )
    if not isinstance(raw_namespace, Mapping):
        return {}
    result: dict[str, set[str]] = {}
    tools = raw_namespace.get("tools")
    if not isinstance(tools, Mapping):
        return result
    for tool_name, raw_tool in tools.items():
        if not isinstance(raw_tool, Mapping):
            continue
        nested = raw_tool.get("operations")
        if isinstance(nested, Mapping) and nested:
            for operation, raw_policy in nested.items():
                name = str(operation or "").strip()
                if name:
                    result[name] = operation_grants(
                        raw_policy if isinstance(raw_policy, Mapping) else {},
                        raw_tool,
                    )
            continue
        name = str(raw_tool.get("operation") or tool_name or "").strip()
        if name:
            result[name] = operation_grants(raw_tool, {})
    return result


def _merge_authority_map(
    base: Mapping[str, Iterable[str]],
    extra: Mapping[str, Iterable[str]],
) -> dict[str, list[str]]:
    merged = {key: set(_strings(values)) for key, values in dict(base or {}).items()}
    for key, values in dict(extra or {}).items():
        merged.setdefault(str(key), set()).update(_strings(values))
    return {key: sorted(values) for key, values in merged.items() if values}


def _merge_named_authority(
    base: Mapping[str, Any] | str | None,
    extra: Mapping[str, Any],
) -> Mapping[str, Any] | str:
    if base == "*":
        return "*"
    merged: dict[str, dict[str, list[str]]] = {}
    for source in (base or {}, extra or {}):
        if not isinstance(source, Mapping):
            continue
        for resource, namespaces in source.items():
            if not isinstance(namespaces, Mapping):
                continue
            for namespace, operations in namespaces.items():
                selected = merged.setdefault(str(resource), {}).setdefault(
                    str(namespace), []
                )
                raw_operations = (
                    [operations]
                    if isinstance(operations, str)
                    else operations
                    if isinstance(operations, (list, tuple, set, frozenset))
                    else ()
                )
                for operation in raw_operations:
                    value = str(operation or "").strip()
                    if value and value not in selected:
                        selected.append(value)
    return {
        resource: {
            namespace: operations
            for namespace, operations in namespaces.items()
            if operations
        }
        for resource, namespaces in merged.items()
        if any(namespaces.values())
    }


def _resolved_from_card(authority: CardAuthority) -> ResolvedCardAuthority:
    """Use a live Control Card as the preset for a newly attached Agent Card."""

    return ResolvedCardAuthority(
        resource_grants={
            resource: list(grants)
            for resource, grants in authority.resource_grants.items()
        },
        resource_operations={
            resource: list(operations)
            for resource, operations in authority.resource_operations.items()
        },
        operations=list(authority.operations),
        named_service_operations=authority.named_service_operations,
        named_services=copy.deepcopy(dict(authority.named_services or {})),
        account_scope={},
        identity_scope=authority.identity_scope,
        properties=copy.deepcopy(dict(authority.properties or {})),
    )


def _descriptor_standard_maps(
    *,
    catalog_config: Any,
    descriptor_payload: Mapping[str, Any],
    selection: AgentCapabilityPolicy | None,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, Any]]:
    """Resolve consumer requests through the active provider-owned catalog."""

    if descriptor_payload.get("standard_authority_overridden") is True:
        return {}, {}, {}
    request = descriptor_payload.get("standard_authority")
    if request is None:
        return {}, {}, {}
    if not isinstance(request, Mapping):
        raise ValueError("agent_descriptor_standard_authority_invalid")

    grants: dict[str, set[str]] = {}
    operations: dict[str, set[str]] = {}
    named: dict[str, dict[str, set[str]]] = {}

    selected_servers = (
        set(selection.capabilities.get("mcp_servers", ()))
        if selection is not None
        else None
    )
    for raw in request.get("resources") or ():
        if not isinstance(raw, Mapping):
            raise ValueError("agent_descriptor_resource_request_invalid")
        server_id = str(raw.get("server_id") or "").strip()
        resource = str(raw.get("resource") or "").strip()
        if not server_id or not resource:
            raise ValueError("agent_descriptor_resource_request_invalid")
        if selected_servers is not None and server_id not in selected_servers:
            continue
        configured = catalog_config.card_selector_config(resource)
        if configured is None:
            raise ValueError(f"unknown delegated resource: {resource}")
        resource_key = str(configured.resource or "").strip()
        offered_tools = {
            str(tool.name or "").strip(): tool
            for tool in configured.tools or ()
            if str(tool.name or "").strip()
        }
        requested = _strings(raw.get("operations"))
        if selection is not None:
            selected_tools = _capability_children(selection, "mcp_tools", server_id)
            requested = [name for name in requested if name == "*" or name in selected_tools]
            if "*" in _strings(raw.get("operations")):
                requested = sorted(selected_tools)
        selected_operations = (
            sorted(offered_tools)
            if "*" in requested
            else requested
        )
        unknown = sorted(set(selected_operations) - set(offered_tools))
        if unknown:
            raise ValueError(
                f"unknown delegated operation(s) for {resource_key!r}: "
                + ", ".join(unknown)
            )
        selected_grants = set(_strings(raw.get("grants")))
        for operation in selected_operations:
            selected_grants.update(offered_tools[operation].grants or ())
        allowed_grants = set(catalog_config.supported_scopes(resource_key))
        if not selected_grants.issubset(allowed_grants):
            raise ValueError(f"delegated resource grants invalid for {resource_key!r}")
        if selected_grants:
            grants.setdefault(resource_key, set()).update(selected_grants)
        if selected_operations:
            operations.setdefault(resource_key, set()).update(selected_operations)

    requested_namespaces = request.get("named_services") or ()
    selected_namespaces = (
        set(selection.capabilities.get("named_services", ()))
        if selection is not None
        else None
    )
    for raw in requested_namespaces:
        if not isinstance(raw, Mapping):
            raise ValueError("agent_descriptor_named_service_request_invalid")
        namespace = str(raw.get("namespace") or "").strip().lower().rstrip(":")
        if not namespace:
            raise ValueError("agent_descriptor_named_service_request_invalid")
        if selected_namespaces is not None and namespace not in selected_namespaces:
            continue
        declared = _strings(raw.get("operations"))
        selected_inner = (
            _capability_children(
                selection, "named_service_operations", namespace
            )
            if selection is not None
            else None
        )
        candidates: list[tuple[Any, set[str], dict[str, set[str]]]] = []
        for configured in catalog_config.resources:
            if not isinstance(configured.named_services, Mapping):
                continue
            offered = configured_named_service_operations(configured.named_services)
            if namespace not in offered:
                continue
            matched = {
                operation
                for operation in offered[namespace]
                if any(_operation_matches(wanted, operation) for wanted in declared)
                and (
                    selected_inner is None
                    or any(
                        _operation_matches(wanted, operation)
                        for wanted in selected_inner
                    )
                )
            }
            if matched:
                candidates.append(
                    (
                        configured,
                        matched,
                        _named_operation_grants(configured.named_services, namespace),
                    )
                )
        if len(candidates) != 1:
            raise ValueError(
                f"named-service namespace {namespace!r} resolves to "
                f"{len(candidates)} catalog resources"
            )
        configured, matched, grants_by_operation = candidates[0]
        resource_key = str(configured.resource or "").strip()
        resource_grants = grants.setdefault(resource_key, set())
        for operation in matched:
            resource_grants.update(grants_by_operation.get(operation, ()))
        operations.setdefault(resource_key, set()).update(
            _named_service_outer_operations(
                matched,
                (tool.name for tool in configured.tools or ()),
            )
        )
        named.setdefault(resource_key, {}).setdefault(namespace, set()).update(matched)

    return (
        {resource: sorted(values) for resource, values in grants.items() if values},
        {resource: sorted(values) for resource, values in operations.items() if values},
        {
            resource: {
                namespace: sorted(values)
                for namespace, values in namespaces.items()
                if values
            }
            for resource, namespaces in named.items()
            if any(namespaces.values())
        },
    )


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
    selected_resource_grants: Mapping[str, Any] | None = None,
    selected_resource_operations: Mapping[str, Any] | None = None,
    selected_named_service_operations: Mapping[str, Any] | str | None = None,
    properties: Mapping[str, Any] | None = None,
    issuer_label: str = "",
    manage_url: str = "",
) -> dict[str, Any]:
    """Materialize one descriptor as an administrator Control Card preset.

    The Control Card and per-user Agent Card have stable ids. A new descriptor
    revision rematerializes the preset; repeated sync of the same revision
    preserves administrator edits. The Agent Card changes only on first
    attachment or explicit replacement, so new capabilities stay unselected.
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
            defaults=requested_selection,
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
        catalog_config = await service._catalog_config(
            active,
            owner_subject=grantor_subject,
        )
        descriptor_grants, descriptor_operations, descriptor_named = (
            _descriptor_standard_maps(
                catalog_config=catalog_config,
                descriptor_payload=descriptor_payload,
                selection=None,
            )
        )
        control_grants = _merge_authority_map(
            resource_grants or {},
            descriptor_grants,
        )
        control_operations = _merge_authority_map(
            resource_operations or {},
            descriptor_operations,
        )
        control_named = _merge_named_authority(
            named_service_operations,
            descriptor_named,
        )
        resolved = await resolve_agent_descriptor_standard_authority(
            service,
            active=active,
            owner_subject=grantor_subject,
            resource_grants=control_grants,
            resource_operations=control_operations,
            named_service_operations=control_named,
            properties=descriptor_properties,
        )
        selected_standard = None
        if any(
            value is not None
            for value in (
                selected_resource_grants,
                selected_resource_operations,
                selected_named_service_operations,
            )
        ) or requested_selection is not None:
            default_grants, default_operations, default_named = (
                _descriptor_standard_maps(
                    catalog_config=catalog_config,
                    descriptor_payload=descriptor_payload,
                    selection=requested_selection,
                )
                if requested_selection is not None
                else ({}, {}, {})
            )
            selected_standard = await resolve_agent_descriptor_standard_authority(
                service,
                active=active,
                owner_subject=grantor_subject,
                resource_grants=_merge_authority_map(
                    selected_resource_grants or {},
                    default_grants,
                ),
                resource_operations=_merge_authority_map(
                    selected_resource_operations or {},
                    default_operations,
                ),
                named_service_operations=_merge_named_authority(
                    selected_named_service_operations,
                    default_named,
                ),
                properties=descriptor_properties,
            )
    except (AgentCapabilityPolicyError, ValueError) as exc:
        return {
            "ok": False,
            "error": getattr(exc, "reason", "agent_descriptor_authority_invalid"),
            "message": str(exc),
            "status": 400,
        }
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
    if selected_standard is not None and selected_standard.error is not None:
        return selected_standard.error

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

    now = int(time.time())
    existing_descriptor = (
        dict(existing_control.resource_acceptance or {}).get(agent_resource)
        if existing_control is not None
        else None
    )
    descriptor_is_current = (
        existing_descriptor is not None
        and existing_descriptor.kind == AGENT_DESCRIPTOR_ACCEPTANCE_KIND
        and existing_descriptor.revision == descriptor_revision
    )
    if descriptor_is_current:
        control_authority = card_authority_from_record(existing_control)
        control_changed = False
    else:
        control_changed = True
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
        control_authority = CardAuthority(
            access_id=control_id,
            client_id=f"control-card:{AGENT_DESCRIPTOR_ISSUER_KIND}",
            grantor_subject=grantor_subject,
            delegate_subject="",
            source=ACCESS_SOURCE_CONTROL,
            card_kind=CARD_KIND_CONTROL,
            label=_clean(issuer_label) or f"{application} / {agent_id}",
            card_revision=(
                existing_control.card_revision + 1
                if existing_control is not None
                else 1
            ),
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

    try:
        authority = AgentCapabilityPolicy.from_property(
            control_authority.properties[AGENT_CAPABILITY_AUTHORITY_PROPERTY]
        )
        raw_defaults = control_authority.properties.get(
            AGENT_CAPABILITY_DEFAULTS_PROPERTY
        )
        defaults = (
            AgentCapabilityPolicy.from_property(raw_defaults)
            if raw_defaults is not None
            else AgentCapabilityPolicy.empty(agent_resource)
        )
    except (KeyError, AgentCapabilityPolicyError) as exc:
        return {
            "ok": False,
            "error": getattr(exc, "reason", "agent_capability_authority_missing"),
            "status": 409,
        }

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
                if requested_selection is not None and not descriptor_is_current
                else defaults
                if existing_resident is None
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
    initial_standard = (
        _resolved_from_card(control_authority)
        if descriptor_is_current
        else (selected_standard or resolved)
    )
    if existing_resident is None:
        resident_acceptance = next_resource_acceptance(
            resources=initial_standard.resource_grants,
            row_for=lambda resource: service._configured_resource(
                resource,
                config=catalog_config,
            ),
            catalog_version=catalog_version,
            selected_operations=initial_standard.resource_operations,
        )
        resident_acceptance[agent_resource] = descriptor_evidence
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
            operations=tuple(initial_standard.operations),
            resource_grants={
                key: tuple(value)
                for key, value in initial_standard.resource_grants.items()
            },
            resource_operations={
                key: tuple(value)
                for key, value in initial_standard.resource_operations.items()
            },
            named_service_operations=initial_standard.named_service_operations,
            named_services=copy.deepcopy(initial_standard.named_services),
            account_scope={},
            identity_scope=initial_standard.identity_scope or "grantor",
            catalog_version=catalog_version,
            card_revision=1,
            created_at=now,
            expires_at=now + AGENT_CAPABILITY_CARD_LEASE_SECONDS,
            source=ACCESS_SOURCE_AGENT,
            resource_acceptance=resident_acceptance,
            control_card=binding,
            properties=resident_selection_properties(
                {},
                selection=selected,
            ),
        )
        expected_resident_revision = 0
    else:
        acceptance = dict(existing_resident.resource_acceptance or {})
        replace_standard = replace_selection and selected_standard is not None
        if replace_standard:
            acceptance = preserve_descriptor_acceptance(
                acceptance,
                next_resource_acceptance(
                    resources=selected_standard.resource_grants,
                    row_for=lambda resource: service._configured_resource(
                        resource,
                        config=catalog_config,
                    ),
                    catalog_version=catalog_version,
                    selected_operations=selected_standard.resource_operations,
                    previous=existing_resident.resource_acceptance,
                ),
            )
        if replace_selection or agent_resource not in acceptance:
            acceptance[agent_resource] = descriptor_evidence
        replacements: dict[str, Any] = {
            "card_revision": existing_resident.card_revision + 1,
            "catalog_version": (
                catalog_version
                if replace_selection
                else existing_resident.catalog_version
            ),
            "expires_at": (
                now + AGENT_CAPABILITY_CARD_LEASE_SECONDS
                if existing_resident.expires_at <= now
                and not existing_resident.access_token
                else existing_resident.expires_at
            ),
            "resource_acceptance": acceptance,
            "control_card": binding,
            "properties": resident_selection_properties(
                existing_resident.properties,
                selection=selected,
            ),
        }
        if replace_standard:
            replacements.update(
                operations=tuple(selected_standard.operations),
                resource_grants={
                    key: tuple(value)
                    for key, value in selected_standard.resource_grants.items()
                },
                resource_operations={
                    key: tuple(value)
                    for key, value in selected_standard.resource_operations.items()
                },
                named_service_operations=(
                    selected_standard.named_service_operations
                ),
                named_services=copy.deepcopy(selected_standard.named_services),
                identity_scope=selected_standard.identity_scope or "grantor",
            )
        resident = dataclasses.replace(existing_resident, **replacements)
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


async def update_agent_capability_selection(
    service: Any,
    user: Mapping[str, Any],
    *,
    access_id: str,
    selected_capabilities: Mapping[str, Any],
    expected_card_revision: int | None,
) -> dict[str, Any]:
    """Replace one resident Agent Card's visible descriptor selection.

    The current linked Control Card supplies the editable ceiling. Values that
    disappeared from that ceiling remain stored but hidden, while a submitted
    value outside it can never be added. The exact Card revision is mandatory
    so a browser cannot overwrite a concurrent descriptor sync or user edit.
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
    access_id = _clean(access_id)
    if not access_id:
        return {
            "ok": False,
            "error": "delegated_access_requires_access_id",
            "status": 400,
        }
    if expected_card_revision is None:
        return {
            "ok": False,
            "error": "agent_capability_card_revision_required",
            "status": 400,
        }

    try:
        loaded_resident = await service._load_record_any_state(
            access_id,
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
    if loaded_resident is None:
        return {"ok": False, "error": "delegated_access_not_found", "status": 404}
    resident, resident_state = loaded_resident
    if resident_state != CARD_STATE_ACTIVE:
        return {"ok": False, "error": "agent_capability_card_not_active", "status": 409}
    if (
        resident.source != ACCESS_SOURCE_AGENT
        or resident.card_kind != CARD_KIND_AGENT
        or not is_resident_client_id(resident.client_id)
        or resident.access_id
        != stable_resident_access_id(grantor_subject, resident.client_id)
        or resident.control_card is None
        or AGENT_CAPABILITY_SELECTION_PROPERTY
        not in dict(resident.properties or {})
    ):
        return {
            "ok": False,
            "error": "agent_capability_card_required",
            "status": 409,
        }
    if int(expected_card_revision) != int(resident.card_revision):
        return {
            "ok": False,
            "error": "agent_capability_card_revision_conflict",
            "expected": int(expected_card_revision),
            "actual": int(resident.card_revision),
            "access": resident.to_public_dict(),
            "status": 409,
        }

    control_id = resident.control_card.control_id
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
    if loaded_control is None or loaded_control[1] != CARD_STATE_ACTIVE:
        return {
            "ok": False,
            "error": "agent_capability_control_not_active",
            "status": 409,
        }
    control = loaded_control[0]
    if (
        not _record_is_credentialless(control)
        or control.card_kind != CARD_KIND_CONTROL
        or control.issuer_kind != AGENT_DESCRIPTOR_ISSUER_KIND
        or control.access_id != control_id
    ):
        return {
            "ok": False,
            "error": "agent_capability_control_identity_conflict",
            "status": 409,
        }

    try:
        authority = AgentCapabilityPolicy.from_property(
            dict(control.properties or {}).get(
                AGENT_CAPABILITY_AUTHORITY_PROPERTY
            )
        )
        requested = AgentCapabilityPolicy.from_property(selected_capabilities)
        raw_current = dict(resident.properties or {}).get(
            AGENT_CAPABILITY_SELECTION_PROPERTY
        )
        current = (
            AgentCapabilityPolicy.from_property(raw_current)
            if raw_current is not None
            else AgentCapabilityPolicy.empty(authority.resource)
        )
        selected = replace_visible_selection(
            current=current,
            authority=authority,
            requested=requested,
        )
    except AgentCapabilityPolicyError as exc:
        return {"ok": False, "error": exc.reason, "status": 400}

    acceptance = dict(resident.resource_acceptance or {})
    descriptor_evidence = dict(control.resource_acceptance or {}).get(
        authority.resource
    )
    if descriptor_evidence is not None:
        acceptance[authority.resource] = descriptor_evidence
    updated = dataclasses.replace(
        resident,
        card_revision=resident.card_revision + 1,
        catalog_version=control.catalog_version,
        resource_acceptance=acceptance,
        properties=resident_selection_properties(
            resident.properties,
            selection=selected,
        ),
    )
    if updated.properties == resident.properties and (
        updated.catalog_version == resident.catalog_version
        and updated.resource_acceptance == resident.resource_acceptance
    ):
        updated = resident
        changed = False
    else:
        try:
            await service._persist_record(
                updated,
                expected_revision=resident.card_revision,
            )
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except CardConflict as exc:
            return {
                "ok": False,
                "error": "agent_capability_card_revision_conflict",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 409,
            }
        except (CardUnavailable, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "agent_capability_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        changed = True
        await service.notify_change(
            grantor_subject,
            action="updated",
            access=updated.to_public_dict(),
        )

    try:
        projection = AgentCapabilityPolicy.from_property(
            effective_card_authority(
                card_authority_from_record(updated),
                card_authority_from_record(control),
            ).properties[AGENT_CAPABILITY_PROJECTION_PROPERTY]
        )
    except (ControlCardMismatch, AgentCapabilityPolicyError) as exc:
        return {
            "ok": False,
            "error": getattr(exc, "reason", "agent_capability_projection_failed"),
            "status": 409,
        }
    return {
        "ok": True,
        "access": updated.to_public_dict(),
        "selection": selected.to_property(),
        "projection": projection.to_property(),
        "card_changed": changed,
    }


__all__ = [
    "AGENT_CAPABILITY_CARD_LEASE_SECONDS",
    "resolve_agent_descriptor_standard_authority",
    "sync_agent_capability_control",
    "update_agent_capability_selection",
]

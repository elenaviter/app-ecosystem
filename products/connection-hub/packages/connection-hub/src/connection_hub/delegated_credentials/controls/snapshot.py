# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Exact, catalog-bounded authority snapshots for credentialless Control Cards."""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Mapping

from connection_hub.agent_account_scope import normalize_account_scope
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.drift import (
    selected_named_service_operations,
)
from connection_hub.delegated_credentials.resource_operations import operation_union

CONTROL_SNAPSHOT_PROPERTY = "connection_hub.control_snapshot"
CONTROL_SNAPSHOT_SCHEMA = "connection_hub.control_snapshot.v1"
CONTROL_SNAPSHOT_MODE_EXACT = "exact"
CONTROL_SNAPSHOT_STATE_EXACT = "exact"
CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED = "review_required"

APPLICATION_OPERATIONS_PROPERTY = "kdcube.application_operations"
APPLICATION_OPERATIONS_SCHEMA = "kdcube.application_operations.v1"
APPLICATION_OPERATIONS_MODE_SELECTED = "selected"
APPLICATION_API_RESOURCE = "*"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _property(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    item = value.get(name)
    return item if isinstance(item, Mapping) else {}


def application_operations_are_explicit(properties: Any) -> bool:
    policy = _property(properties, APPLICATION_OPERATIONS_PROPERTY)
    return (
        _clean(policy.get("schema")) == APPLICATION_OPERATIONS_SCHEMA
        and _clean(policy.get("mode")) == APPLICATION_OPERATIONS_MODE_SELECTED
    )


def control_snapshot_metadata(properties: Any) -> Mapping[str, Any]:
    return _property(properties, CONTROL_SNAPSHOT_PROPERTY)


def control_snapshot_wildcards(authority: CardAuthority) -> tuple[str, ...]:
    """Authority dimensions that still mean an open-ended future selection."""

    found: set[str] = set()
    for resource, grants in authority.resource_grants.items():
        if "*" in grants:
            found.add(f"resource_grants:{resource}")
    for resource, operations in authority.resource_operations.items():
        if "*" in operations:
            found.add(f"resource_operations:{resource}")

    selection = authority.named_service_operations
    if selection.is_all:
        found.add("named_service_operations")
    elif selection.is_unknown:
        found.add("named_service_operations:unknown")
    elif selection.is_exact:
        for resource, namespaces in selection.operations.items():
            for namespace, operations in namespaces.items():
                if "*" in operations:
                    found.add(f"named_service_operations:{resource}:{namespace}")

    for provider, accounts in normalize_account_scope(authority.account_scope).items():
        if provider == "*":
            found.add("account_scope:*")
        for account_id, claims in accounts.items():
            if account_id == "*" or not claims or "*" in claims:
                found.add(f"account_scope:{provider}:{account_id}")

    if (
        APPLICATION_API_RESOURCE in authority.resource_grants
        and not application_operations_are_explicit(authority.properties)
    ):
        found.add("application_operations")
    return tuple(sorted(found))


def control_snapshot_is_exact(authority: CardAuthority) -> bool:
    metadata = control_snapshot_metadata(authority.properties)
    return (
        _clean(metadata.get("schema")) == CONTROL_SNAPSHOT_SCHEMA
        and _clean(metadata.get("mode")) == CONTROL_SNAPSHOT_MODE_EXACT
        and _clean(metadata.get("state"))
        in {CONTROL_SNAPSHOT_STATE_EXACT, CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED}
        and bool(_clean(metadata.get("basis_catalog_version")))
        and not control_snapshot_wildcards(authority)
    )


def _accepted_values(authority: CardAuthority, resource: str, field: str) -> tuple[str, ...]:
    accepted = authority.resource_acceptance.get(resource)
    if accepted is None:
        return ()
    value = getattr(accepted, field, None)
    if field == "operations" and isinstance(value, Mapping):
        return tuple(sorted({_clean(item) for item in value if _clean(item)}))
    return tuple(sorted({_clean(item) for item in (value or ()) if _clean(item)}))


def _exact_values(
    authority: CardAuthority,
    values: tuple[str, ...],
    *,
    resource: str,
    field: str,
    unresolved: set[str],
) -> tuple[str, ...]:
    selected = tuple(sorted({_clean(item) for item in values if _clean(item)}))
    if "*" not in selected:
        return selected
    materialized = _accepted_values(authority, resource, field)
    if not materialized:
        unresolved.add(f"{field}:{resource}")
    return materialized


def materialize_control_snapshot(
    authority: CardAuthority,
    *,
    basis_catalog_version: str,
    origin: str,
    source_card_revision: int | None = None,
) -> CardAuthority:
    """Replace every open-ended Control Card choice with historical evidence.

    Missing evidence becomes an empty exact selection and is named in the
    snapshot metadata. That dimension is therefore denied until the owner
    reviews and saves it; it is never expanded against today's catalog.
    """

    unresolved: set[str] = set()
    resource_grants = {
        resource: _exact_values(
            authority,
            tuple(grants),
            resource=resource,
            field="grants",
            unresolved=unresolved,
        )
        for resource, grants in authority.resource_grants.items()
    }
    resource_operations = {
        resource: _exact_values(
            authority,
            tuple(operations),
            resource=resource,
            field="operations",
            unresolved=unresolved,
        )
        for resource, operations in authority.resource_operations.items()
    }

    selection = authority.named_service_operations
    if selection.is_all:
        materialized = selected_named_service_operations(authority)
        if not materialized and authority.resource_grants:
            unresolved.add("named_service_operations")
    elif selection.is_exact:
        materialized = {
            resource: {
                namespace: {
                    _clean(operation)
                    for operation in operations
                    if _clean(operation) and _clean(operation) != "*"
                }
                for namespace, operations in namespaces.items()
            }
            for resource, namespaces in selection.operations.items()
        }
        for resource, namespaces in selection.operations.items():
            for namespace, operations in namespaces.items():
                if "*" in operations:
                    unresolved.add(
                        f"named_service_operations:{resource}:{namespace}"
                    )
    else:
        materialized = {}
    named_service_operations = NamedServiceSelection.exact(
        {
            resource: {
                namespace: sorted(operations)
                for namespace, operations in namespaces.items()
            }
            for resource, namespaces in materialized.items()
        }
    )

    account_scope: dict[str, dict[str, tuple[str, ...]]] = {}
    for provider, accounts in normalize_account_scope(authority.account_scope).items():
        if provider == "*":
            unresolved.add("account_scope:*")
            continue
        for account_id, claims in accounts.items():
            if account_id == "*":
                unresolved.add(f"account_scope:{provider}:*")
                continue
            exact_claims = tuple(sorted(claim for claim in claims if claim != "*"))
            if not claims or "*" in claims:
                unresolved.add(f"account_scope:{provider}:{account_id}")
            if exact_claims:
                account_scope.setdefault(provider, {})[account_id] = exact_claims

    properties = copy.deepcopy(dict(authority.properties or {}))
    if APPLICATION_API_RESOURCE in resource_grants:
        if not application_operations_are_explicit(properties):
            # Before this marker existed, the application row was unrestricted;
            # any stored operation names were display data, not an exact policy.
            # Its historical API catalog is owned by KDCube rather than the CH
            # catalog, so no past revision can safely materialize it here.
            resource_operations[APPLICATION_API_RESOURCE] = ()
            unresolved.add("application_operations")
            properties[APPLICATION_OPERATIONS_PROPERTY] = {
                "schema": APPLICATION_OPERATIONS_SCHEMA,
                "mode": APPLICATION_OPERATIONS_MODE_SELECTED,
            }

    basis = _clean(basis_catalog_version) or _clean(authority.catalog_version)
    metadata: dict[str, Any] = {
        "schema": CONTROL_SNAPSHOT_SCHEMA,
        "mode": CONTROL_SNAPSHOT_MODE_EXACT,
        "state": (
            CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED
            if unresolved
            else CONTROL_SNAPSHOT_STATE_EXACT
        ),
        "basis_catalog_version": basis,
        "origin": _clean(origin) or "created",
    }
    if source_card_revision is not None:
        metadata["source_card_revision"] = max(0, int(source_card_revision))
    if unresolved:
        metadata["review_required"] = sorted(unresolved)
    properties[CONTROL_SNAPSHOT_PROPERTY] = metadata

    provenance = copy.deepcopy(dict(authority.provenance or {}))
    if metadata["origin"] != "created":
        provenance["control_snapshot_migration"] = {
            "basis_catalog_version": basis,
            "source_card_revision": metadata.get("source_card_revision", 0),
            "review_required": sorted(unresolved),
        }

    return dataclasses.replace(
        authority,
        catalog_version=basis,
        operations=operation_union(resource_operations),
        resource_grants=resource_grants,
        resource_operations=resource_operations,
        named_service_operations=named_service_operations,
        account_scope=account_scope,
        properties=properties,
        provenance=provenance,
    )


def reviewed_control_snapshot_properties(
    properties: Any,
    *,
    basis_catalog_version: str,
) -> dict[str, Any]:
    """Server-owned marker written after an exact operator-reviewed save."""

    result = copy.deepcopy(dict(properties or {}))
    result[CONTROL_SNAPSHOT_PROPERTY] = {
        "schema": CONTROL_SNAPSHOT_SCHEMA,
        "mode": CONTROL_SNAPSHOT_MODE_EXACT,
        "state": CONTROL_SNAPSHOT_STATE_EXACT,
        "basis_catalog_version": _clean(basis_catalog_version),
        "origin": "reviewed",
    }
    return result


def fail_closed_control_snapshot(
    authority: CardAuthority,
    *,
    basis_catalog_version: str,
    reason: str,
) -> CardAuthority:
    """Create an editable deny-all revision when history cannot be trusted."""

    empty = dataclasses.replace(
        authority,
        operations=(),
        resource_grants={},
        resource_operations={},
        named_service_operations=NamedServiceSelection.none(),
        named_services={},
        account_scope={},
        resource_acceptance={},
    )
    exact = materialize_control_snapshot(
        empty,
        basis_catalog_version=basis_catalog_version,
        origin="historical_boundary_unavailable",
    )
    properties = copy.deepcopy(dict(exact.properties or {}))
    metadata = dict(control_snapshot_metadata(properties))
    metadata["state"] = CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED
    metadata["review_required"] = [_clean(reason) or "historical_boundary"]
    properties[CONTROL_SNAPSHOT_PROPERTY] = metadata
    provenance = copy.deepcopy(dict(exact.provenance or {}))
    provenance["control_snapshot_migration"] = {
        "basis_catalog_version": _clean(basis_catalog_version),
        "review_required": list(metadata["review_required"]),
    }
    return dataclasses.replace(exact, properties=properties, provenance=provenance)


__all__ = [
    "APPLICATION_API_RESOURCE",
    "APPLICATION_OPERATIONS_MODE_SELECTED",
    "APPLICATION_OPERATIONS_PROPERTY",
    "APPLICATION_OPERATIONS_SCHEMA",
    "CONTROL_SNAPSHOT_MODE_EXACT",
    "CONTROL_SNAPSHOT_PROPERTY",
    "CONTROL_SNAPSHOT_SCHEMA",
    "CONTROL_SNAPSHOT_STATE_EXACT",
    "CONTROL_SNAPSHOT_STATE_REVIEW_REQUIRED",
    "application_operations_are_explicit",
    "control_snapshot_is_exact",
    "control_snapshot_metadata",
    "control_snapshot_wildcards",
    "fail_closed_control_snapshot",
    "materialize_control_snapshot",
    "reviewed_control_snapshot_properties",
]

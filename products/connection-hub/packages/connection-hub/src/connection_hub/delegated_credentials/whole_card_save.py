# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""Non-writing whole-Card comparison and exact added-operation mode checks.

The caller loads the durable current Card, checks its pin FIRST, then reads
the active catalog and checks the render pin. No policy is prepared here;
the real CardVersion STAGE owns every effect and its one change identity.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .managed_card_edit_forward import ManagedCardEditError, invocation_modes_part
from .managed_grants import managed_operations
from .cards.model import NamedServiceSelection
from .named_service_policy import configured_named_service_operations
from .resource_operations import normalize_resource_grants, normalize_resource_operations


class WholeCardSaveRefused(ManagedCardEditError):
    def __init__(self, reason: str, status: int = 409, message: str = "", **details: Any):
        super().__init__(reason, status, message)
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), **copy.deepcopy(self.details)}


def check_card_pin(original: Any, expected: Any) -> None:
    if type(expected) is not int or expected < 1:
        raise WholeCardSaveRefused("managed_card_edit_revision_required", message=
                                  "Reload the Card before saving; your draft is kept.")
    if original.card_revision != expected:
        raise WholeCardSaveRefused("managed_card_edit_revision_changed", message=
                                  "The Card changed. Reload it before saving; your draft is kept.")


def check_catalog_pin(expected: Any, active: str) -> None:
    if type(expected) is not str or not expected or expected != expected.strip():
        raise WholeCardSaveRefused("consent_catalog_version_required", message=
                                  "Reload the catalog before saving; your draft is kept.")
    if expected != active:
        raise WholeCardSaveRefused("consent_catalog_changed", message=
            "The catalog changed. Reload it before saving; your draft is kept.",
            expected_catalog_version=expected, active_catalog_version=active)


def normalize_selection(original: Any, selection: Mapping[str, Any], *, config: Any) -> dict[str, Any]:
    """Freeze supplied dimensions; expand All under this exact active catalog.

    Missing dimensions stay missing (the candidate builder retains them).
    A wildcard with no enumerable catalog operations refuses rather than
    persisting a permission that would silently grow with a later catalog.
    """
    result = copy.deepcopy(dict(selection))
    try:
        grants = normalize_resource_grants(result.get("resource_grants", original.resource_grants))
        if "resource_grants" in result:
            result["resource_grants"] = {key: list(value) for key, value in grants.items()}
        if "resource_operations" in result:
            operations = normalize_resource_operations(result["resource_operations"])
            concrete = {}
            for resource, names in operations.items():
                if "*" in names:
                    offered = {tool.name for tool in config.tools_for_scopes(
                        list(grants.get(resource, ())), resource=resource) if tool.name and tool.name != "*"}
                    if not offered:
                        raise WholeCardSaveRefused("card_selection_wildcard_unavailable")
                    names = tuple(sorted((set(names) - {"*"}) | offered))
                concrete[resource] = list(names)
            result["resource_operations"] = concrete
        if "named_service_operations" in result:
            named = NamedServiceSelection.from_stored(result["named_service_operations"], present=True)
            if named.is_unknown:
                raise WholeCardSaveRefused("managed_card_edit_request_invalid", 400)
            # A screen's All is consent to the pinned catalog, not a durable
            # wildcard which could grow later. The managed carrier stays the
            # existing concrete resource/namespace/operation map.
            selected = {resource: {} for resource in grants} if named.is_all else named.operations
            concrete_named = {}
            for resource, namespaces in selected.items():
                if named.is_all or any("*" in names for names in namespaces.values()):
                    row = config.card_selector_config(resource)
                    if row is None:
                        raise WholeCardSaveRefused("card_selection_wildcard_unavailable")
                    offered = configured_named_service_operations(
                        row.named_services, grants=grants.get(resource, ()))
                if named.is_all:
                    namespaces = offered
                concrete_namespaces = {}
                for namespace, names in namespaces.items():
                    if "*" in names:
                        available = offered.get(namespace, set())
                        if not available:
                            raise WholeCardSaveRefused("card_selection_wildcard_unavailable")
                        names = (set(names) - {"*"}) | available
                    concrete_namespaces[namespace] = sorted(set(names))
                if concrete_namespaces:
                    concrete_named[resource] = concrete_namespaces
            result["named_service_operations"] = concrete_named
    except (TypeError, ValueError) as exc:
        if isinstance(exc, WholeCardSaveRefused):
            raise
        raise WholeCardSaveRefused("managed_card_edit_request_invalid", 400) from None
    return result


def added_invocation_modes(*, original: Any, candidate: Any, config: Any, modes: Any,
                           policy_bearing: bool) -> dict[str, dict[str, str]]:
    """Exactly one choice per net added policy-governed outer operation.

    Controls, the all-application API resource, and catalog application-owned
    operations carry no invocation modes. Held, removed and unknown operation
    keys refuse; they cannot reset/replenish a policy as a Save side effect.
    """
    try:
        submitted = invocation_modes_part(modes)
    except ManagedCardEditError as exc:
        raise WholeCardSaveRefused(exc.reason, exc.status) from None
    added = {}
    if policy_bearing:
        for resource, names in candidate.resource_operations.items():
            if resource == "*":  # APPLICATION_API_RESOURCE: the application decides the role
                continue
            row = config.card_selector_config(resource)
            owned = managed_operations(row)
            new = sorted(set(names) - set(original.resource_operations.get(resource, ())) - owned)
            if new:
                added[resource] = new
    expected = {(resource, name) for resource, names in added.items() for name in names}
    supplied = {(resource, name) for resource, names in submitted.items() for name in names}
    if supplied - expected:
        raise WholeCardSaveRefused("invocation_modes_invalid", 400)
    missing = {resource: [name for name in names if (resource, name) not in supplied]
               for resource, names in added.items()}
    missing = {resource: names for resource, names in missing.items() if names}
    if missing:
        raise WholeCardSaveRefused("invocation_mode_required", message=
            "Choose Once or Every time beside every added permission; your draft is kept.",
            missing_invocation_modes=missing)
    return submitted


def same_selection(original: Any, selection: Mapping[str, Any], *, label: Any = None,
                   composition_mode: Any = None) -> bool:
    """Compare visible fields only; no catalog/audit timestamp is restamped to make a change."""
    def unordered(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: unordered(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)) and all(type(item) is str for item in value):
            return sorted(set(value))
        return value

    for field, value in selection.items():
        stored = getattr(original, field)
        if field == "named_service_operations":
            if stored.is_all or stored.is_unknown:
                # Compare with the stored expansion, never re-read today's
                # catalog to redefine the permission the original Card held.
                stored = {
                    resource: {namespace: sorted(names) for namespace, names in
                               configured_named_service_operations(
                                   original.named_services, grants=grants).items()}
                    for resource, grants in original.resource_grants.items()
                }
                stored = {resource: namespaces for resource, namespaces in stored.items() if namespaces}
            else:
                stored = stored.to_stored()
        if (value != stored if field == "properties" else unordered(value) != unordered(stored)):
            return False
    return ((label is None or label == original.label)
            and (composition_mode is None or (composition_mode or "and") == (original.composition_mode or "and")))


def checked_properties(original: Any, submitted: Any, *, own_my: bool) -> dict[str, Any]:
    if own_my:
        from .my_card_person_properties_plan import whole_person_properties_patch
        try:
            whole_person_properties_patch(dict(original.properties or {}), submitted)
        except ValueError:
            raise WholeCardSaveRefused("project_person_my_card_property_invalid", 400) from None
    elif submitted != dict(original.properties or {}):
        raise WholeCardSaveRefused("managed_card_edit_fields_unsupported")
    return copy.deepcopy(dict(submitted))

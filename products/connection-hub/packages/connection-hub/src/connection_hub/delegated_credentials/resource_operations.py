# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Resource-qualified outer-operation authority.

An outer operation is an MCP tool name or a managed REST operation. Tool names
are local to a protected resource, so a card must retain the resource that made
each name available. The flat operation list remains a derived compatibility
view; authorization reads this map.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any, Iterable, Mapping


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError("resource operation selection must be a list")
    return tuple(sorted({_clean(item) for item in values if _clean(item)}))


def normalize_resource(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    return raw.split("?", 1)[0].rstrip("/")


def resource_matches(credential_resource: str, request_resource: str) -> bool:
    credential_resource = normalize_resource(credential_resource)
    request_resource = normalize_resource(request_resource)
    if not credential_resource or not request_resource:
        return False
    return credential_resource == request_resource or fnmatch(
        request_resource, credential_resource
    )


def normalize_resource_operations(value: Any) -> dict[str, tuple[str, ...]]:
    """Normalize ``{resource: [operation]}`` without dropping empty choices."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("resource_operations must be an object")
    out: dict[str, tuple[str, ...]] = {}
    for raw_resource, operations in value.items():
        resource = _clean(raw_resource)
        if not resource:
            continue
        out[resource] = _string_tuple(operations)
    return out


def normalize_resource_grants(value: Any) -> dict[str, tuple[str, ...]]:
    """Normalize ``{resource: [grant]}`` for storage and credential issuance."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("resource_grants must be an object")
    out: dict[str, tuple[str, ...]] = {}
    for raw_resource, grants in value.items():
        resource = _clean(raw_resource)
        if not resource:
            continue
        out[resource] = _string_tuple(grants)
    return out


def project_legacy_operations(
    resource_grants: Mapping[str, Any],
    operations: Iterable[Any],
) -> dict[str, tuple[str, ...]]:
    """Give every selected resource the flat authority an old card carried."""
    selected = _string_tuple(list(operations or ()))
    projected = {
        _clean(resource): selected
        for resource in resource_grants
        if _clean(resource)
    }
    # Pre-resource OAuth records treated their flat operation list as applying
    # to the request's resource. Preserve that meaning as wildcard authority
    # until the card is next written with a concrete resource selection.
    if not projected and selected:
        projected["*"] = selected
    return projected


def operation_union(
    resource_operations: Mapping[str, Iterable[Any]],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _clean(operation)
                for operations in resource_operations.values()
                for operation in (operations or ())
                if _clean(operation)
            }
        )
    )


def operations_for_resource(
    resource_operations: Mapping[str, Iterable[Any]],
    request_resource: str,
    *,
    matched_resource: str = "",
) -> tuple[str, ...]:
    """Operations attached to the resource selected for this request.

    ``matched_resource`` is the card key chosen by the resource boundary and is
    preferred to a second wildcard match. The fallback is useful to projections
    that only know the concrete request URL.
    """
    normalized = normalize_resource_operations(resource_operations)
    selected_key = _clean(matched_resource)
    if selected_key:
        if selected_key in normalized:
            return normalized[selected_key]
        selected_normalized = normalize_resource(selected_key)
        for resource, operations in normalized.items():
            if normalize_resource(resource) == selected_normalized:
                return operations
        return ()

    selected: set[str] = set()
    for resource, operations in normalized.items():
        if resource_matches(resource, request_resource):
            selected.update(operations)
    return tuple(sorted(selected))


__all__ = [
    "normalize_resource",
    "normalize_resource_grants",
    "normalize_resource_operations",
    "operation_union",
    "operations_for_resource",
    "project_legacy_operations",
    "resource_matches",
]

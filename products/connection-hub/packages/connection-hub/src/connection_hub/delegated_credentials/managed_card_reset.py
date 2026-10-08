# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""The displayed result of a person's per-service Reset to Control.

One pure function, shared by the Hub (which shows the result before the person
confirms) and by the project host (which recomputes it from its own signed read
and commits only the result whose digest the person saw). Given the person's
My Card and its current effective Control, one service's operations, grants and
named services become the Control's; every other service, the Card's identity
and its credentials are unchanged (``reset_candidate``). Nothing is written.
"""

from __future__ import annotations

from typing import Any

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from .caller_writer_gate import reset_candidate
from .cards.model import CardAuthority

DISPLAY_SCHEMA = "managed-card-reset-display.v1"
RESET_FIELDS = ("resource_grants", "resource_operations", "named_service_operations")


class ManagedCardResetError(ValueError):
    pass


def _service(value: dict[str, Any], resource: str) -> dict[str, Any]:
    named = value.get("named_service_operations")
    return {"operations": sorted((value.get("resource_operations") or {}).get(resource, ())),
            "grants": sorted((value.get("resource_grants") or {}).get(resource, ())),
            "named_services": named.get(resource) if isinstance(named, dict) else named}


def managed_card_reset_display(my: CardAuthority, control: CardAuthority, *,
                               resource: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(changed selection fields, display, display digest) for exactly one service.

    ``control`` is the My Card's current effective Control (its chain composed
    with each Control's own AND/OR). Raises ``CallerWriteRefused`` when the
    service is not held, and ``ManagedCardResetError`` when nothing would change.
    """
    named = control.named_service_operations
    candidate = reset_candidate(my, resource=resource,
        control_operations=(control.resource_operations or {}).get(resource, ()),
        control_grants=(control.resource_grants or {}).get(resource, ()),
        control_named_services=None if (named.is_all or named.is_unknown) else dict(named.operations).get(resource))
    before, after = _service(my.to_dict(), resource), _service(candidate, resource)
    if before == after:
        raise ManagedCardResetError("managed_card_reset_unchanged")
    display = {"schema": DISPLAY_SCHEMA, "access_id": my.access_id, "original_revision": my.card_revision,
               "resource": resource, "before": before, "after": after}
    return ({field: candidate[field] for field in RESET_FIELDS}, display,
            sha256_hex(canonical_json_bytes(display)))


__all__ = ["DISPLAY_SCHEMA", "RESET_FIELDS", "ManagedCardResetError", "managed_card_reset_display"]

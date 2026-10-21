# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A person's custom MCP connectors on their agent's Card change only by that person (W918).

Any admin of a project the agent attends may edit the agent's Card, but "the
actual "custom mcp services" to not fall into that category" (operator,
2026-10-21). An admin may still remove one: "i think it is reasonable to allow
admin to remove this as well".

So a Save by anyone but the Card's grantor leaves every connector resource
(``urn:connection-hub:remote-mcp:<id>``) either exactly as it was, or removes
it entirely. Comparison is per connector over everything that carries
authority for it (grants, selected tools, accepted descriptors, invocation
modes), never over flattened grants: every connector shares
``external_mcp:use``.
"""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.remote_mcp.models import connector_id_from_resource

CUSTOM_MCP_OWNER_ONLY = "card_custom_mcp_owner_only"


def is_connector(resource: Any) -> bool:
    return bool(connector_id_from_resource(resource))


def _acceptance(card: Any, resource: str) -> Any:
    accepted = dict(getattr(card, "resource_acceptance", None) or {}).get(resource)
    return accepted  # a frozen dataclass: compared by value


def _connector_state(card: Any, resource: str) -> tuple:
    return (
        tuple(sorted(set(dict(card.resource_grants or {}).get(resource, ())))),
        tuple(sorted(set(dict(card.resource_operations or {}).get(resource, ())))),
        _acceptance(card, resource),
    )


def custom_mcp_owner_only_refusal(*, original: Any, candidate: Any, actor_subject: str,
                                  invocation_modes: Mapping[str, Any] | None = None,
                                  accepted_operations: Mapping[str, Any] | None = None) -> str | None:
    """``card_custom_mcp_owner_only`` when a non-grantor Save adds or changes a connector; else None.

    The grantor is read from the stored Card, never from the request.
    """
    if str(actor_subject or "") == str(original.grantor_subject or ""):
        return None
    before = {r for r in (original.resource_grants or {}) if is_connector(r)}
    for resource in (candidate.resource_grants or {}):
        if not is_connector(resource):
            continue
        if resource not in before or _connector_state(candidate, resource) != _connector_state(original, resource):
            return CUSTOM_MCP_OWNER_ONLY
    for resource in (candidate.resource_operations or {}):
        if is_connector(resource) and resource not in (candidate.resource_grants or {}) and resource not in before:
            return CUSTOM_MCP_OWNER_ONLY
    for carrier in (invocation_modes, accepted_operations):
        if any(is_connector(resource) for resource in dict(carrier or {})):
            return CUSTOM_MCP_OWNER_ONLY
    return None


__all__ = ["CUSTOM_MCP_OWNER_ONLY", "custom_mcp_owner_only_refusal", "is_connector"]

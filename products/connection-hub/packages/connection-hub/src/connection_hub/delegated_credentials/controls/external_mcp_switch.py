# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The External MCP switch as a Card field (W918).

This module owns the switch itself: its property name, its two values, the
codec check, and the refusal of any write that does not come through the
owning Control's transaction. What the switch admits on a project chain is
decided in ``controls.project_mcp_family``.
"""

from __future__ import annotations

from typing import Any, Mapping

EXTERNAL_MCP_FAMILY_PROPERTY = "external_mcp_family"
FAMILY_ON = "on"
FAMILY_OFF = "off"
FAMILY_VALUES = frozenset({FAMILY_ON, FAMILY_OFF})
DIRECT_WRITE_REFUSED = "external_mcp_family_requires_project_transaction"


def validate_family_property(properties: Mapping[str, Any]) -> None:
    """Card codec check: the switch is ``on`` or ``off`` when present."""
    if EXTERNAL_MCP_FAMILY_PROPERTY in properties and properties[EXTERNAL_MCP_FAMILY_PROPERTY] not in FAMILY_VALUES:
        raise ValueError("control_card_external_mcp_family_invalid")


def direct_switch_write_refusal(value: Any) -> dict[str, Any] | None:
    """The refusal for a switch value on a direct (non-transaction) Control write; None when none is asked."""
    if value is None:
        return None
    return {"ok": False, "error": DIRECT_WRITE_REFUSED, "status": 409,
            "message": "Only the project's own Control edit sets External MCP. Nothing was saved."}


__all__ = [
    "DIRECT_WRITE_REFUSED", "EXTERNAL_MCP_FAMILY_PROPERTY", "FAMILY_OFF", "FAMILY_ON", "FAMILY_VALUES",
    "direct_switch_write_refusal", "validate_family_property",
]

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
# W937: the project's A2A switch, the same shape for the caller's own remote
# A2A connectors. Absent is off.
A2A_FAMILY_PROPERTY = "a2a_family"
FAMILY_ON = "on"
FAMILY_OFF = "off"
FAMILY_VALUES = frozenset({FAMILY_ON, FAMILY_OFF})
DIRECT_WRITE_REFUSED = "external_mcp_family_requires_project_transaction"
A2A_DIRECT_WRITE_REFUSED = "a2a_family_requires_project_transaction"
# Every project switch property, written only by its Control's own transaction.
SWITCH_PROPERTIES = (EXTERNAL_MCP_FAMILY_PROPERTY, A2A_FAMILY_PROPERTY)
_SWITCH_LABELS = {EXTERNAL_MCP_FAMILY_PROPERTY: "External MCP", A2A_FAMILY_PROPERTY: "A2A agents"}
_DIRECT_WRITE_CODES = {EXTERNAL_MCP_FAMILY_PROPERTY: DIRECT_WRITE_REFUSED, A2A_FAMILY_PROPERTY: A2A_DIRECT_WRITE_REFUSED}


def validate_family_property(properties: Mapping[str, Any]) -> None:
    """Card codec check: each switch is ``on`` or ``off`` when present."""
    for switch_property in SWITCH_PROPERTIES:
        if switch_property in properties and properties[switch_property] not in FAMILY_VALUES:
            raise ValueError(f"control_card_{switch_property}_invalid")


def direct_switch_write_refusal(value: Any, *, switch_property: str = EXTERNAL_MCP_FAMILY_PROPERTY) -> dict[str, Any] | None:
    """The refusal for a switch value on a direct (non-transaction) Control write; None when none is asked."""
    if value is None:
        return None
    return {"ok": False, "error": _DIRECT_WRITE_CODES[switch_property], "status": 409,
            "message": f"Only the project's own Control edit sets {_SWITCH_LABELS[switch_property]}. Nothing was saved."}


__all__ = [
    "A2A_DIRECT_WRITE_REFUSED", "A2A_FAMILY_PROPERTY", "DIRECT_WRITE_REFUSED", "EXTERNAL_MCP_FAMILY_PROPERTY",
    "SWITCH_PROPERTIES", "FAMILY_OFF", "FAMILY_ON", "FAMILY_VALUES",
    "direct_switch_write_refusal", "validate_family_property",
]

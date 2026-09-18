# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Exact application targets allowed for delegated conversation reads."""

from __future__ import annotations

from typing import Any, Mapping

CONVERSATION_TARGETS_PROPERTY = "kdcube.conversation_targets"


def conversation_targets(properties: Mapping[str, Any] | None) -> tuple[str, ...]:
    raw = (properties or {}).get(CONVERSATION_TARGETS_PROPERTY)
    if not isinstance(raw, (list, tuple)):
        return ()
    values: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value or value != value.strip() or value == "*":
            return ()
        values.add(value)
    return tuple(sorted(values))


def compose_conversation_targets(
    caller: Mapping[str, Any] | None,
    control: Mapping[str, Any] | None,
    *,
    mode: str,
) -> tuple[str, ...]:
    left = set(conversation_targets(caller))
    right = set(conversation_targets(control))
    return tuple(sorted(left | right if mode == "or" else left & right))


__all__ = [
    "CONVERSATION_TARGETS_PROPERTY",
    "compose_conversation_targets",
    "conversation_targets",
]

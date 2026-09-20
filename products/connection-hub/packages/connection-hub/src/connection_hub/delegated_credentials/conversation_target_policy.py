# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Application/agent targets allowed for delegated conversation reads."""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.delegated_credentials.application_resources import (
    APPLICATION_RESOURCE_PREFIX,
    ApplicationResource,
    ApplicationResourceError,
)

CONVERSATION_TARGETS_PROPERTY = "kdcube.conversation_targets"


def conversation_targets(properties: Mapping[str, Any] | None) -> tuple[str, ...]:
    raw = (properties or {}).get(CONVERSATION_TARGETS_PROPERTY)
    if not isinstance(raw, (list, tuple)):
        return ()
    values: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value or value != value.strip() or value == "*":
            return ()
        if value.startswith(APPLICATION_RESOURCE_PREFIX):
            try:
                values.add(ApplicationResource.parse(value).resource)
            except ApplicationResourceError:
                return ()
        else:
            # Legacy Cards named an exact application id. Keep that bounded
            # representation readable while new writers use typed resources.
            if any(marker in value for marker in ("*", "?", "[", "]")):
                return ()
            values.add(value)
    return tuple(sorted(values))


def _intersection(left: str, right: str) -> str | None:
    left_typed = left.startswith(APPLICATION_RESOURCE_PREFIX)
    right_typed = right.startswith(APPLICATION_RESOURCE_PREFIX)
    if not left_typed and not right_typed:
        return left if left == right else None
    if left_typed and right_typed:
        intersection = ApplicationResource.parse(left).intersection(
            ApplicationResource.parse(right)
        )
        return intersection.resource if intersection is not None else None

    typed = ApplicationResource.parse(left if left_typed else right)
    legacy = right if left_typed else left
    if typed.application not in ("*", legacy):
        return None
    return ApplicationResource(
        tenant=typed.tenant,
        project=typed.project,
        application=legacy,
        agent=typed.agent,
    ).resource


def compose_conversation_targets(
    caller: Mapping[str, Any] | None,
    control: Mapping[str, Any] | None,
    *,
    mode: str,
) -> tuple[str, ...]:
    left = set(conversation_targets(caller))
    right = set(conversation_targets(control))
    if mode == "or":
        return tuple(sorted(left | right))
    return tuple(
        sorted(
            {
                value
                for caller_target in left
                for control_target in right
                if (value := _intersection(caller_target, control_target)) is not None
            }
        )
    )


__all__ = [
    "CONVERSATION_TARGETS_PROPERTY",
    "compose_conversation_targets",
    "conversation_targets",
]

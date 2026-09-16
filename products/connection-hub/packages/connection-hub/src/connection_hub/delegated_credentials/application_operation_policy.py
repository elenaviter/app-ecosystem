# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Application-operation role policy stored on a Connection Hub Card."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


APPLICATION_API_RESOURCE = "*"
APPLICATION_OPERATIONS_PROPERTY = "kdcube.application_operations"
APPLICATION_OPERATIONS_SCHEMA_V1 = "kdcube.application_operations.v1"
APPLICATION_OPERATIONS_SCHEMA_V2 = "kdcube.application_operations.v2"
APPLICATION_OPERATIONS_MODE_SELECTED = "selected"

REGISTERED_ROLE = "kdcube:role:registered"
PAID_ROLE = "kdcube:role:paid"
PRIVILEGED_ROLE = "kdcube:role:privileged"
ADMIN_ROLE = "kdcube:role:admin"
SUPER_ADMIN_ROLE = "kdcube:role:super-admin"

# Keep this ordering aligned with KDCube's authorization gates. The legacy
# admin role is the same tier as privileged rather than a separate elevation.
_PLATFORM_ROLE_RANK = {
    REGISTERED_ROLE: 0,
    PAID_ROLE: 1,
    PRIVILEGED_ROLE: 2,
    ADMIN_ROLE: 2,
    SUPER_ADMIN_ROLE: 3,
}


class ApplicationOperationPolicyError(ValueError):
    """The Card declares an application policy that cannot be enforced."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        source: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set, frozenset)):
        source = value
    else:
        source = ()
    return tuple(dict.fromkeys(item for raw in source if (item := _clean(raw))))


def platform_role_rank(role: Any) -> int | None:
    return _PLATFORM_ROLE_RANK.get(_clean(role))


def platform_role_allowed_by(
    role: Any,
    allowed_roles: Iterable[Any],
) -> bool:
    """Return whether one configured platform-role ceiling admits ``role``."""

    value = _clean(role)
    allowed = {_clean(candidate) for candidate in allowed_roles if _clean(candidate)}
    if value in allowed:
        return True
    rank = platform_role_rank(value)
    if rank is None:
        return False
    return any(
        allowed_rank is not None and rank <= allowed_rank
        for candidate in allowed
        if (allowed_rank := platform_role_rank(candidate)) is not None
    )


def _require_platform_role(role: Any, *, reason: str) -> str:
    value = _clean(role)
    if value not in _PLATFORM_ROLE_RANK:
        raise ApplicationOperationPolicyError(reason)
    return value


def strongest_platform_role(roles: Iterable[Any] | None) -> str:
    values = tuple(
        role
        for raw in (roles or ())
        if (role := _clean(raw)) in _PLATFORM_ROLE_RANK
    )
    if not values:
        return ""
    return max(values, key=lambda role: (_PLATFORM_ROLE_RANK[role], role))


def _same_tier_role(left: str, right: str) -> str:
    if left == right:
        return left
    if {left, right} == {ADMIN_ROLE, PRIVILEGED_ROLE}:
        return PRIVILEGED_ROLE
    return sorted((left, right))[0]


def weaker_platform_role(left: Any, right: Any) -> str:
    left_role = _require_platform_role(left, reason="application_default_role_invalid")
    right_role = _require_platform_role(right, reason="application_default_role_invalid")
    left_rank = _PLATFORM_ROLE_RANK[left_role]
    right_rank = _PLATFORM_ROLE_RANK[right_role]
    if left_rank == right_rank:
        return _same_tier_role(left_role, right_role)
    return left_role if left_rank < right_rank else right_role


def stronger_platform_role(left: Any, right: Any) -> str:
    left_role = _require_platform_role(left, reason="application_default_role_invalid")
    right_role = _require_platform_role(right, reason="application_default_role_invalid")
    left_rank = _PLATFORM_ROLE_RANK[left_role]
    right_rank = _PLATFORM_ROLE_RANK[right_role]
    if left_rank == right_rank:
        return _same_tier_role(left_role, right_role)
    return left_role if left_rank > right_rank else right_role


@dataclass(frozen=True)
class ApplicationOperationRolePolicy:
    """One default role plus exact overrides for selected operations."""

    default_role: str
    operation_roles: Mapping[str, str] = field(default_factory=dict)
    schema: str = APPLICATION_OPERATIONS_SCHEMA_V2

    def role_for(self, operation_ref: Any) -> str:
        return self.operation_roles.get(_clean(operation_ref), self.default_role)

    def to_property(self) -> dict[str, Any]:
        return {
            "schema": APPLICATION_OPERATIONS_SCHEMA_V2,
            "mode": APPLICATION_OPERATIONS_MODE_SELECTED,
            "default_role": self.default_role,
            "operation_roles": dict(sorted(self.operation_roles.items())),
        }


def application_operation_policy_declared(properties: Any) -> bool:
    return isinstance(properties, Mapping) and APPLICATION_OPERATIONS_PROPERTY in properties


def application_operation_policy_enabled(properties: Any) -> bool:
    if not isinstance(properties, Mapping):
        return False
    policy = properties.get(APPLICATION_OPERATIONS_PROPERTY)
    if not isinstance(policy, Mapping):
        return False
    return (
        _clean(policy.get("schema"))
        in {APPLICATION_OPERATIONS_SCHEMA_V1, APPLICATION_OPERATIONS_SCHEMA_V2}
        and _clean(policy.get("mode")) == APPLICATION_OPERATIONS_MODE_SELECTED
    )


def application_operation_role_policy(
    properties: Any,
    *,
    resource_grants: Mapping[str, Any] | None = None,
) -> ApplicationOperationRolePolicy | None:
    """Parse a declared policy, raising when its meaning is not enforceable.

    Version 1 stored only the selected-operation marker. Its default role is
    inferred from the strongest platform role on the historical application
    resource row. Version 2 stores one explicit default and exact overrides.
    """

    if not application_operation_policy_declared(properties):
        return None
    raw = properties.get(APPLICATION_OPERATIONS_PROPERTY)
    if not isinstance(raw, Mapping):
        raise ApplicationOperationPolicyError("application_operation_policy_invalid")
    if _clean(raw.get("mode")) != APPLICATION_OPERATIONS_MODE_SELECTED:
        raise ApplicationOperationPolicyError("application_operation_policy_mode_invalid")

    schema = _clean(raw.get("schema"))
    if schema == APPLICATION_OPERATIONS_SCHEMA_V1:
        grants = resource_grants if isinstance(resource_grants, Mapping) else {}
        default_role = strongest_platform_role(_values(grants.get(APPLICATION_API_RESOURCE)))
        if not default_role:
            raise ApplicationOperationPolicyError("application_default_role_missing")
        return ApplicationOperationRolePolicy(
            default_role=default_role,
            operation_roles={},
            schema=schema,
        )
    if schema != APPLICATION_OPERATIONS_SCHEMA_V2:
        raise ApplicationOperationPolicyError("application_operation_policy_schema_invalid")

    default_role = _require_platform_role(
        raw.get("default_role"),
        reason="application_default_role_invalid",
    )
    raw_operation_roles = raw.get("operation_roles")
    if raw_operation_roles is None:
        raw_operation_roles = {}
    if not isinstance(raw_operation_roles, Mapping):
        raise ApplicationOperationPolicyError("application_operation_roles_invalid")
    operation_roles: dict[str, str] = {}
    for raw_operation, raw_role in raw_operation_roles.items():
        operation = _clean(raw_operation)
        if not operation:
            raise ApplicationOperationPolicyError("application_operation_override_invalid")
        role = _require_platform_role(
            raw_role,
            reason="application_operation_override_role_invalid",
        )
        if role != default_role:
            operation_roles[operation] = role
    return ApplicationOperationRolePolicy(
        default_role=default_role,
        operation_roles=operation_roles,
        schema=schema,
    )


def validate_application_operation_role_policy(
    policy: ApplicationOperationRolePolicy,
    *,
    selected_operations: Iterable[Any],
    delegable_roles: Iterable[Any] | None = None,
    allowed_roles: Iterable[Any] | None = None,
) -> None:
    selected = {_clean(value) for value in selected_operations if _clean(value)}
    stale_overrides = sorted(set(policy.operation_roles) - selected)
    if stale_overrides:
        raise ApplicationOperationPolicyError(
            "application_operation_override_not_selected"
        )
    requested = {policy.default_role, *policy.operation_roles.values()}
    if delegable_roles is not None:
        delegable = {_clean(value) for value in delegable_roles if _clean(value)}
        if requested - delegable:
            raise ApplicationOperationPolicyError("application_roles_not_delegable")
    if allowed_roles is not None:
        allowed = tuple(allowed_roles)
        if any(not platform_role_allowed_by(role, allowed) for role in requested):
            raise ApplicationOperationPolicyError(
                "application_roles_not_allowed_for_resource"
            )


def compose_application_operation_role_policy(
    card: ApplicationOperationRolePolicy,
    control: ApplicationOperationRolePolicy,
    *,
    card_operations: Iterable[Any],
    control_operations: Iterable[Any],
    composition_mode: str,
) -> tuple[ApplicationOperationRolePolicy, tuple[str, ...]]:
    """Compose exact operation-to-role maps for a linked Control Card."""

    card_selected = {_clean(value) for value in card_operations if _clean(value)}
    control_selected = {
        _clean(value) for value in control_operations if _clean(value)
    }
    if _clean(composition_mode).lower() == "or":
        selected = card_selected | control_selected
        default_role = stronger_platform_role(card.default_role, control.default_role)
    else:
        selected = card_selected & control_selected
        default_role = weaker_platform_role(card.default_role, control.default_role)

    roles: dict[str, str] = {}
    for operation in sorted(selected):
        in_card = operation in card_selected
        in_control = operation in control_selected
        if in_card and in_control:
            role = (
                stronger_platform_role(card.role_for(operation), control.role_for(operation))
                if _clean(composition_mode).lower() == "or"
                else weaker_platform_role(card.role_for(operation), control.role_for(operation))
            )
        elif in_card:
            role = card.role_for(operation)
        else:
            role = control.role_for(operation)
        if role != default_role:
            roles[operation] = role

    return (
        ApplicationOperationRolePolicy(
            default_role=default_role,
            operation_roles=roles,
        ),
        tuple(sorted(selected)),
    )


__all__ = [
    "ADMIN_ROLE",
    "APPLICATION_API_RESOURCE",
    "APPLICATION_OPERATIONS_MODE_SELECTED",
    "APPLICATION_OPERATIONS_PROPERTY",
    "APPLICATION_OPERATIONS_SCHEMA_V1",
    "APPLICATION_OPERATIONS_SCHEMA_V2",
    "ApplicationOperationPolicyError",
    "ApplicationOperationRolePolicy",
    "PAID_ROLE",
    "PRIVILEGED_ROLE",
    "REGISTERED_ROLE",
    "SUPER_ADMIN_ROLE",
    "application_operation_policy_declared",
    "application_operation_policy_enabled",
    "application_operation_role_policy",
    "compose_application_operation_role_policy",
    "platform_role_allowed_by",
    "platform_role_rank",
    "stronger_platform_role",
    "strongest_platform_role",
    "validate_application_operation_role_policy",
    "weaker_platform_role",
]

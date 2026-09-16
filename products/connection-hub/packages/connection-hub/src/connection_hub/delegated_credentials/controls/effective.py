# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Apply one optional, credentialless Control Card to a caller Card."""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Mapping

from connection_hub.agent_account_scope import (
    normalize_account_scope,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_API_RESOURCE,
    APPLICATION_OPERATIONS_PROPERTY,
    ApplicationOperationPolicyError,
    ApplicationOperationRolePolicy,
    application_operation_policy_enabled,
    application_operation_role_policy,
    compose_application_operation_role_policy,
    strongest_platform_role,
)
from connection_hub.delegated_credentials.catalog.drift import (
    selected_named_service_operations,
)
from connection_hub.delegated_credentials.named_service_policy import (
    merge_named_service_configs,
    narrow_named_service_config,
    operation_grants,
)
from connection_hub.delegated_credentials.controls.model import (
    ProjectControlCardAuthority,
    control_card_from_legacy,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    control_snapshot_is_exact,
)


class ControlCardMismatch(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _intersect_values(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    left_values = set(left)
    right_values = set(right)
    if "*" in left_values:
        return tuple(sorted(right_values))
    if "*" in right_values:
        return tuple(sorted(left_values))
    return tuple(sorted(left_values & right_values))


def _union_values(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    values = set(left) | set(right)
    if "*" in values:
        return ("*",)
    return tuple(sorted(values))


def _selection_map(value: Any) -> dict[str, dict[str, set[str]]]:
    selection = value.named_service_operations
    if selection.is_none:
        return {}
    if selection.is_exact:
        return {
            resource: {
                namespace: set(operations)
                for namespace, operations in namespaces.items()
            }
            for resource, namespaces in selection.operations.items()
        }
    if isinstance(value, CardAuthority):
        return selected_named_service_operations(value)
    raise ControlCardMismatch("control_card_named_service_selection_invalid")


def _intersect_named_services(
    card: CardAuthority,
    control: Any,
    resources: Mapping[str, tuple[str, ...]],
) -> tuple[NamedServiceSelection, dict[str, Any]]:
    card_map = _selection_map(card)
    control_map = _selection_map(control)
    selected: dict[str, dict[str, list[str]]] = {}
    for resource in resources:
        card_namespaces = card_map.get(resource, {})
        control_namespaces = control_map.get(resource, {})
        for namespace in set(card_namespaces) & set(control_namespaces):
            operations = sorted(
                set(card_namespaces[namespace]) & set(control_namespaces[namespace])
            )
            if operations:
                selected.setdefault(resource, {})[namespace] = operations

    # ``card.named_services`` is already the catalog tree bounded when the Card
    # was saved. Filtering that tree must not collapse provider operations just
    # because their claims live in ``account_scope`` instead of the door's
    # ``resource_grants``. Supply the requirements declared by this already-
    # bounded tree while selecting operations; live account enforcement remains
    # a separate, independently intersected decision.
    materialized_claims = _materialized_claims(card.named_services)
    materialized: dict[str, Any] = {}
    for resource, namespaces in selected.items():
        narrowed = narrow_named_service_config(
            config=card.named_services,
            selected=namespaces,
            grants=set(resources.get(resource, ())) | materialized_claims,
            resource=resource,
        )
        materialized = merge_named_service_configs(materialized, narrowed)
    return NamedServiceSelection.exact(selected), materialized


def _union_named_services(
    card: CardAuthority,
    control: CardAuthority,
    resources: Mapping[str, tuple[str, ...]],
) -> tuple[NamedServiceSelection, dict[str, Any]]:
    card_map = _selection_map(card)
    control_map = _selection_map(control)
    selected: dict[str, dict[str, list[str]]] = {}
    for resource in resources:
        for source in (card_map, control_map):
            for namespace, operations in source.get(resource, {}).items():
                target = selected.setdefault(resource, {}).setdefault(namespace, [])
                for operation in sorted(operations):
                    if operation not in target:
                        target.append(operation)

    materialized: dict[str, Any] = {}
    for authority, selection in ((card, card_map), (control, control_map)):
        claims = _materialized_claims(authority.named_services)
        for resource, namespaces in selection.items():
            if resource not in resources:
                continue
            narrowed = narrow_named_service_config(
                config=authority.named_services,
                selected={
                    namespace: sorted(operations)
                    for namespace, operations in namespaces.items()
                },
                grants=set(resources.get(resource, ())) | claims,
                resource=resource,
            )
            materialized = merge_named_service_configs(materialized, narrowed)
    return NamedServiceSelection.exact(selected), materialized


def _materialized_claims(config: Mapping[str, Any]) -> set[str]:
    claims: set[str] = set()
    namespaces = config.get("namespaces") if isinstance(config, Mapping) else None
    if not isinstance(namespaces, Mapping):
        return claims
    for raw_namespace in namespaces.values():
        if not isinstance(raw_namespace, Mapping):
            continue
        tools = raw_namespace.get("tools")
        if not isinstance(tools, Mapping):
            continue
        for raw_tool in tools.values():
            if not isinstance(raw_tool, Mapping):
                continue
            operations = raw_tool.get("operations")
            if isinstance(operations, Mapping) and operations:
                for raw_operation in operations.values():
                    claims.update(
                        operation_grants(
                            raw_operation if isinstance(raw_operation, Mapping) else {},
                            raw_tool,
                        )
                    )
                continue
            claims.update(operation_grants(raw_tool, {}))
    return claims


def _intersect_accounts(
    card_scope: Mapping[str, Any],
    control_scope: Mapping[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    card = normalize_account_scope(card_scope)
    control = normalize_account_scope(control_scope)
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for provider, card_accounts in card.items():
        control_accounts = control.get(provider) or control.get("*")
        if not control_accounts:
            continue
        for account_id, card_claims in card_accounts.items():
            if account_id == "*" and "*" not in control_accounts:
                candidates = control_accounts.items()
            else:
                held = control_accounts.get(account_id) or control_accounts.get("*")
                candidates = ((account_id, held),) if held is not None else ()
            for effective_account, control_claims in candidates:
                if control_claims is None:
                    continue
                claims = _intersect_values(tuple(card_claims), tuple(control_claims))
                if claims:
                    result.setdefault(provider, {})[effective_account] = claims
    return result


def _union_accounts(
    card_scope: Mapping[str, Any],
    control_scope: Mapping[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    result = normalize_account_scope(card_scope)
    for provider, accounts in normalize_account_scope(control_scope).items():
        target = result.setdefault(provider, {})
        for account_id, claims in accounts.items():
            target[account_id] = _union_values(
                tuple(target.get(account_id, ())),
                tuple(claims),
            )
    return result


def _application_policy(authority: CardAuthority):
    try:
        return application_operation_role_policy(
            authority.properties,
            resource_grants=authority.resource_grants,
        )
    except ApplicationOperationPolicyError as exc:
        raise ControlCardMismatch(exc.reason) from exc


def _legacy_application_policy(authority: CardAuthority) -> ApplicationOperationRolePolicy:
    """Represent one pre-policy application row without narrowing its operations.

    Before the explicit marker existed, the ``"*"`` resource row carried a
    role but its operation list was display data rather than an exact allow
    list. The strongest historical role is the same default used by the v1
    policy migration. Callers use this adapter only while composing against an
    exact policy on the other Card; they never persist it back to the legacy
    Card.
    """

    default_role = strongest_platform_role(
        authority.resource_grants.get(APPLICATION_API_RESOURCE, ())
    )
    if not default_role:
        raise ControlCardMismatch("application_default_role_missing")
    return ApplicationOperationRolePolicy(default_role=default_role)


def effective_card_authority(
    card: CardAuthority,
    control: CardAuthority | ProjectControlCardAuthority,
) -> CardAuthority:
    """Compose the caller Card with its current Control Card revision.

    ``and`` is the default and narrows authority. ``or`` contributes authority.
    The result always preserves the caller Card identity and credential life.
    """

    if isinstance(control, ProjectControlCardAuthority):
        control = control_card_from_legacy(control)
    binding = card.control_card
    if not authority_is_credentialless(control):
        raise ControlCardMismatch("control_card_has_credential")
    if not control_snapshot_is_exact(control):
        raise ControlCardMismatch("control_card_exact_snapshot_required")
    if binding is None or binding.control_id != control.access_id:
        raise ControlCardMismatch("control_card_binding_mismatch")
    if binding.issuer_ref != control.issuer_ref:
        raise ControlCardMismatch("control_card_issuer_mismatch")
    if card.grantor_subject != control.grantor_subject:
        raise ControlCardMismatch("control_card_grantor_mismatch")
    card_identity_scope = str(card.identity_scope or "grantor").strip() or "grantor"
    control_identity_scope = (
        str(control.identity_scope or "grantor").strip() or "grantor"
    )
    if card_identity_scope != control_identity_scope:
        raise ControlCardMismatch("control_card_identity_scope_mismatch")
    if control.state != CARD_STATE_ACTIVE:
        raise ControlCardMismatch("control_card_not_active")

    mode = control.composition_mode or CONTROL_COMPOSITION_AND
    if mode == CONTROL_COMPOSITION_OR:
        resource_grants = {
            resource: _union_values(
                tuple(card.resource_grants.get(resource, ())),
                tuple(control.resource_grants.get(resource, ())),
            )
            for resource in set(card.resource_grants) | set(control.resource_grants)
        }
        resource_operations = {
            resource: _union_values(
                tuple(card.resource_operations.get(resource, ())),
                tuple(control.resource_operations.get(resource, ())),
            )
            for resource in resource_grants
        }
    else:
        resource_grants = {}
        for resource, card_grants in card.resource_grants.items():
            ceiling_grants = control.resource_grants.get(resource)
            if ceiling_grants is None:
                continue
            # Presence of the resource is distinct from its claims. An
            # operation can be explicitly claimless, so retaining the shared
            # resource key lets the operation check decide it accurately.
            resource_grants[resource] = _intersect_values(
                tuple(card_grants), tuple(ceiling_grants)
            )
        resource_operations = {
            resource: _intersect_values(
                tuple(card.resource_operations.get(resource, ())),
                tuple(control.resource_operations.get(resource, ())),
            )
            for resource in resource_grants
        }
    properties = copy.deepcopy(
        {**dict(card.properties or {}), **dict(control.properties or {})}
    )
    if APPLICATION_API_RESOURCE in resource_grants:
        card_has_resource = APPLICATION_API_RESOURCE in card.resource_grants
        control_has_resource = APPLICATION_API_RESOURCE in control.resource_grants
        card_policy = _application_policy(card) if card_has_resource else None
        control_policy = _application_policy(control) if control_has_resource else None
        policy_enabled = (
            card_has_resource
            and application_operation_policy_enabled(card.properties)
        ) or (
            control_has_resource
            and application_operation_policy_enabled(control.properties)
        )
        effective_policy = None
        effective_operations: tuple[str, ...] = ()
        if card_policy is not None and control_policy is not None:
            effective_policy, effective_operations = (
                compose_application_operation_role_policy(
                    card_policy,
                    control_policy,
                    card_operations=card.resource_operations.get(
                        APPLICATION_API_RESOURCE, ()
                    ),
                    control_operations=control.resource_operations.get(
                        APPLICATION_API_RESOURCE, ()
                    ),
                    composition_mode=mode,
                )
            )
        elif (
            mode == CONTROL_COMPOSITION_AND
            and card_has_resource
            and card_policy is None
            and control_policy is not None
        ):
            # A pre-policy caller did not make an exact operation selection.
            # AND therefore lets the exact Control Card supply the finite set,
            # while the historical caller role still narrows each projection.
            control_operations = tuple(
                control.resource_operations.get(APPLICATION_API_RESOURCE, ())
            )
            effective_policy, effective_operations = (
                compose_application_operation_role_policy(
                    _legacy_application_policy(card),
                    control_policy,
                    card_operations=control_operations,
                    control_operations=control_operations,
                    composition_mode=mode,
                )
            )
        elif mode == CONTROL_COMPOSITION_OR and card_policy is not None and not control_has_resource:
            effective_policy = card_policy
            effective_operations = tuple(
                card.resource_operations.get(APPLICATION_API_RESOURCE, ())
            )
        elif mode == CONTROL_COMPOSITION_OR and control_policy is not None and not card_has_resource:
            effective_policy = control_policy
            effective_operations = tuple(
                control.resource_operations.get(APPLICATION_API_RESOURCE, ())
            )
        elif policy_enabled:
            raise ControlCardMismatch("application_operation_policy_required")

        if effective_policy is not None:
            resource_grants[APPLICATION_API_RESOURCE] = (
                effective_policy.default_role,
            )
            resource_operations[APPLICATION_API_RESOURCE] = tuple(
                sorted(set(effective_operations))
            )
            properties[APPLICATION_OPERATIONS_PROPERTY] = (
                effective_policy.to_property()
            )
        else:
            # A property on a Card that contributes no application resource is
            # inert metadata, not application authority. Do not let it become
            # an effective policy merely because the two property maps merge.
            properties.pop(APPLICATION_OPERATIONS_PROPERTY, None)
    else:
        properties.pop(APPLICATION_OPERATIONS_PROPERTY, None)
    try:
        if mode == CONTROL_COMPOSITION_OR:
            named_selection, named_services = _union_named_services(
                card, control, resource_grants
            )
            account_scope = _union_accounts(card.account_scope, control.account_scope)
        else:
            named_selection, named_services = _intersect_named_services(
                card, control, resource_grants
            )
            account_scope = _intersect_accounts(card.account_scope, control.account_scope)
    except ControlCardMismatch:
        raise
    except Exception as exc:
        raise ControlCardMismatch("control_card_authority_invalid") from exc
    effective_binding = ControlCardBinding(
        control_id=binding.control_id,
        issuer_ref=binding.issuer_ref,
        issuer_kind=binding.issuer_kind,
        issuer_label=control.issuer_label or binding.issuer_label,
        manage_url=control.manage_url or binding.manage_url,
        control_revision=control.card_revision,
    )
    return dataclasses.replace(
        card,
        operations=(),
        resource_grants=resource_grants,
        resource_operations=resource_operations,
        named_service_operations=named_selection,
        named_services=copy.deepcopy(named_services),
        account_scope=account_scope,
        resource_acceptance=(
            {**dict(card.resource_acceptance), **dict(control.resource_acceptance)}
            if mode == CONTROL_COMPOSITION_OR
            else card.resource_acceptance
        ),
        control_card=effective_binding,
        properties=properties,
    )


__all__ = ["ControlCardMismatch", "effective_card_authority"]

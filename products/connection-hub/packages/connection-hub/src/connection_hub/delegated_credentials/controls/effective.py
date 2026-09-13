# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Intersect a delegated Card with its project-owned control card."""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Mapping

from connection_hub.agent_account_scope import (
    normalize_account_scope,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.drift import (
    selected_named_service_operations,
)
from connection_hub.delegated_credentials.controls.model import (
    CONTROL_CARD_STATE_ACTIVE,
    ProjectControlCardAuthority,
)
from connection_hub.delegated_credentials.named_service_policy import (
    merge_named_service_configs,
    narrow_named_service_config,
    operation_grants,
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
    control: ProjectControlCardAuthority,
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


def effective_card_authority(
    card: CardAuthority,
    control: ProjectControlCardAuthority,
) -> CardAuthority:
    """Return live authority as ``card AND control``.

    The result preserves Card identity and lifetime. It can only remove
    resources, operations, claims, named-service operations, and account scope.
    """

    binding = card.control_card
    if binding is None or binding.control_id != control.control_id:
        raise ControlCardMismatch("control_card_binding_mismatch")
    if binding.issuer_ref != control.issuer_ref:
        raise ControlCardMismatch("control_card_issuer_mismatch")
    if card.grantor_subject != control.grantor_subject:
        raise ControlCardMismatch("control_card_grantor_mismatch")
    if control.state != CONTROL_CARD_STATE_ACTIVE:
        raise ControlCardMismatch("control_card_not_active")

    resource_grants: dict[str, tuple[str, ...]] = {}
    for resource, card_grants in card.resource_grants.items():
        ceiling_grants = control.resource_grants.get(resource)
        if ceiling_grants is None:
            continue
        grants = _intersect_values(tuple(card_grants), tuple(ceiling_grants))
        # Presence of the resource is distinct from its claims. An operation
        # can be explicitly claimless, so retaining the shared resource key is
        # what lets the later operation check decide it accurately.
        resource_grants[resource] = grants

    resource_operations = {
        resource: tuple(
            sorted(
                set(card.resource_operations.get(resource, ()))
                & set(control.resource_operations.get(resource, ()))
            )
        )
        for resource in resource_grants
    }
    try:
        named_selection, named_services = _intersect_named_services(
            card,
            control,
            resource_grants,
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
        control_revision=control.revision,
    )
    return dataclasses.replace(
        card,
        operations=(),
        resource_grants=resource_grants,
        resource_operations=resource_operations,
        named_service_operations=named_selection,
        named_services=copy.deepcopy(named_services),
        account_scope=account_scope,
        control_card=effective_binding,
    )


__all__ = ["ControlCardMismatch", "effective_card_authority"]

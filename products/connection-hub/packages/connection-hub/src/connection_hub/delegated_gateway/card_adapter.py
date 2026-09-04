# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Pure adapter from Card's published read model into Gateway authority."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from connection_hub.delegated_credentials.cards.read_model import (
    OPERATION_STATE_CHANGED,
    OPERATION_STATE_CURRENT,
    OPERATION_STATE_REMOVED,
    OPERATION_STATE_UNKNOWN,
    CardOperationView,
    CardResourceView,
)
from connection_hub.delegated_credentials.cards.read_model import (
    DelegatedCardView as CardReadView,
)
from connection_hub.delegated_gateway.models import (
    RESOURCE_ACTIVE,
    RESOURCE_DISABLED,
    AcceptedDescriptor,
    DelegatedCardView,
    DelegatedResourceEntry,
    GatewayContractError,
    InvocationPolicyView,
    RecoveryLink,
)

CardResourceMetadataResolver = Callable[[CardResourceView], "GatewayResourceMetadata"]


@dataclass(frozen=True)
class GatewayResourceMetadata:
    """Host-owned non-secret metadata intentionally absent from Card storage."""

    endpoint_relation: str
    recovery: tuple[RecoveryLink, ...] = ()
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)


def caller_profile_id_for_card(card: CardReadView) -> str:
    """Stable public caller coordinate paired with the card's access id."""

    if card.profile is not None:
        return card.profile.client_id
    value = str(card.client_id or "").strip()
    if not value:
        raise GatewayContractError("card_caller_profile_id_missing")
    return value


def adapt_card_view(
    card: CardReadView,
    *,
    metadata_for: CardResourceMetadataResolver,
    capabilities: Iterable[str] = (),
) -> DelegatedCardView:
    """Project committed Card authority without reading persistence or secrets.

    Endpoint relations, fixed recovery links, and managed provider locators
    belong to host composition. Requiring ``metadata_for`` keeps those values
    explicit rather than guessing them from a resource URL.
    """

    if not isinstance(card, CardReadView):
        raise GatewayContractError("card_read_view_invalid")
    resources = tuple(
        _adapt_resource(entry, metadata_for(entry)) for entry in card.resources
    )
    return DelegatedCardView(
        caller_type=card.caller_kind,
        caller_profile_id=caller_profile_id_for_card(card),
        access_id=card.access_id,
        card_revision=card.card_revision,
        status=card.state,
        expires_at=card.expires_at,
        source=card.source,
        identity_scope=card.identity_scope,
        grantor_subject=card.grantor_subject,
        resources=resources,
        capabilities=tuple(capabilities),
    )


def _adapt_resource(
    resource: CardResourceView,
    metadata: GatewayResourceMetadata,
) -> DelegatedResourceEntry:
    if not isinstance(resource, CardResourceView):
        raise GatewayContractError("card_resource_view_invalid")
    if not isinstance(metadata, GatewayResourceMetadata):
        raise GatewayContractError("gateway_resource_metadata_invalid")
    operations = tuple(item.name for item in resource.operations)
    accepted_operation_digests = {
        item.name: item.accepted_digest for item in resource.operations
    }
    policies = {
        item.name: _adapt_policy(resource.resource, item)
        for item in resource.operations
        if item.policy is not None
    }
    state, reason = _resource_state(resource)
    return DelegatedResourceEntry(
        resource_id=resource.resource,
        kind=resource.kind,
        provider_id=resource.provider,
        display_label=resource.label or resource.resource,
        endpoint_relation=metadata.endpoint_relation,
        grants=resource.grants,
        operations=operations,
        accepted_descriptor=AcceptedDescriptor(
            revision=resource.accepted_revision,
            digest=resource.accepted_digest,
            operation_digests=accepted_operation_digests,
        ),
        identity_scope=resource.identity_scope,
        state=state,
        unavailable_reason=reason,
        invocation_policies=policies,
        recovery=metadata.recovery,
        provider_metadata=metadata.provider_metadata,
    )


def _adapt_policy(
    resource_id: str, operation: CardOperationView
) -> InvocationPolicyView:
    policy = operation.policy
    if not isinstance(policy, Mapping):
        raise GatewayContractError("card_invocation_policy_invalid")
    authority = policy.get("authority")
    if not isinstance(authority, Mapping):
        raise GatewayContractError("card_invocation_policy_authority_invalid")
    if (
        str(authority.get("resource") or "").strip() != resource_id
        or str(authority.get("operation") or "").strip() != operation.name
        or str(authority.get("surface") or "outer").strip() != "outer"
    ):
        raise GatewayContractError("card_invocation_policy_authority_mismatch")
    return InvocationPolicyView(
        mode=str(policy.get("mode") or "").strip(),
        state=str(policy.get("state") or "").strip(),
        revision=int(policy.get("revision") or 0),
        remaining=(
            None if policy.get("remaining") is None else int(policy.get("remaining"))
        ),
    )


def _resource_state(resource: CardResourceView) -> tuple[str, str]:
    if resource.state in {OPERATION_STATE_CURRENT, OPERATION_STATE_CHANGED}:
        return RESOURCE_ACTIVE, ""
    if resource.state == OPERATION_STATE_REMOVED:
        return RESOURCE_DISABLED, "descriptor_missing"
    if resource.state == OPERATION_STATE_UNKNOWN:
        return RESOURCE_DISABLED, "descriptor_unknown"
    raise GatewayContractError("card_resource_state_invalid")


__all__ = [
    "CardResourceMetadataResolver",
    "GatewayResourceMetadata",
    "adapt_card_view",
    "caller_profile_id_for_card",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Secret-safe caller-self and requestable-resource discovery."""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.resource_operations import (
    resource_matches,
)
from connection_hub.delegated_gateway.models import (
    ACCESS_DESCRIBE_TOOL,
    RESOURCE_ACTIVE,
    DelegatedCardView,
    DelegatedGatewayError,
    GatewayCaller,
    GatewayTool,
    RequestableResource,
    json_copy,
)
from connection_hub.delegated_gateway.ports import (
    GatewayProviderContext,
    RequestableResourceReader,
)
from connection_hub.delegated_gateway.registry import DelegatedMCPProviderRegistry

ACCESS_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "include_requestable": {
            "type": "boolean",
            "description": "Include owner-visible requestable resources when permitted.",
        }
    },
    "additionalProperties": False,
}


def access_describe_tool() -> GatewayTool:
    return GatewayTool(
        name=ACCESS_DESCRIBE_TOOL,
        route=None,
        title="Describe Connection Hub access",
        description=(
            "Describe this caller's current delegated card and granted resources."
        ),
        input_schema=ACCESS_DESCRIBE_SCHEMA,
    )


async def describe_access(
    *,
    caller: GatewayCaller,
    card: DelegatedCardView,
    providers: DelegatedMCPProviderRegistry,
    requestable: RequestableResourceReader | None,
    now: int,
    include_requestable: bool,
) -> dict[str, Any]:
    resources: list[dict[str, Any]] = []
    for resource in sorted(card.resources, key=lambda item: item.resource_id):
        reason = resource.unavailable_reason
        provider_id = resource.provider_id
        provider = None
        current_descriptor: dict[str, Any] | None = None
        try:
            provider = providers.provider_for(resource)
            provider_id = str(provider.provider_id)
        except DelegatedGatewayError as exc:
            reason = exc.reason
        if resource.state != RESOURCE_ACTIVE:
            reason = reason or "resource_disabled"
        elif provider is not None:
            try:
                descriptor = await provider.current_descriptor(
                    GatewayProviderContext(
                        caller=caller,
                        card=card,
                        resource=resource,
                    )
                )
                current_descriptor = {
                    "revision": descriptor.revision,
                    "digest": descriptor.digest,
                    "state": descriptor.state,
                }
                if not descriptor.available:
                    reason = descriptor.unavailable_reason or descriptor.state
                elif any(
                    descriptor.operation_digests.get(operation)
                    != resource.accepted_descriptor.operation_digests.get(operation)
                    for operation in resource.operations
                ):
                    reason = "operation_descriptor_changed"
            except DelegatedGatewayError as exc:
                reason = exc.reason
            except Exception:  # noqa: BLE001 - provider details are secret-safe here
                reason = "resource_provider_unavailable"

        resources.append(
            {
                "resource_id": resource.resource_id,
                "kind": resource.kind,
                "provider_id": provider_id,
                "display_label": resource.display_label,
                "endpoint_relation": resource.endpoint_relation,
                "identity_scope": resource.identity_scope,
                "state": resource.state,
                "grants": list(resource.grants),
                "operations": list(resource.operations),
                "accepted_descriptor": resource.accepted_descriptor.to_public_dict(),
                "current_descriptor": current_descriptor,
                "invocation_policies": {
                    operation: policy.to_public_dict()
                    for operation, policy in sorted(
                        resource.invocation_policies.items()
                    )
                },
                "unavailable_reason": reason,
                "recovery": [item.to_public_dict() for item in resource.recovery],
            }
        )

    payload: dict[str, Any] = {
        "schema": "connection_hub.delegated_gateway.access.v1",
        "caller": {
            "type": caller.caller_type,
            "profile_id": caller.caller_profile_id,
            "access_id": card.access_id,
        },
        "card": {
            "revision": card.card_revision,
            "status": card.status,
            "expires_at": card.expires_at,
            "expired": bool(card.expires_at and card.expires_at <= now),
            "source": card.source,
            "identity_scope": card.identity_scope,
        },
        "resources": resources,
        "requestable_resources": [],
        "requestable_discovery": "not_requested",
    }
    if include_requestable:
        await _add_requestable_resources(
            payload=payload,
            caller=caller,
            card=card,
            requestable=requestable,
        )
    return json_copy(payload, reason="access_description_not_json")


async def _add_requestable_resources(
    *,
    payload: dict[str, Any],
    caller: GatewayCaller,
    card: DelegatedCardView,
    requestable: RequestableResourceReader | None,
) -> None:
    if not card.permits_discovery(caller):
        payload["requestable_discovery"] = "not_permitted"
        return
    if requestable is None:
        payload["requestable_discovery"] = "unavailable"
        return
    payload["requestable_discovery"] = "permitted"
    try:
        candidates = await requestable.list_requestable(caller=caller, card=card)
        if any(not isinstance(item, RequestableResource) for item in candidates):
            raise TypeError("requestable resource contract mismatch")
        visible = [
            item.to_public_dict()
            for item in sorted(candidates, key=lambda value: value.resource_id)
            if _requestable_visible(caller, card, item)
            and item.resource_id not in card.resource_map()
        ]
    except Exception:  # noqa: BLE001 - reader failures must not expose inventory
        payload["requestable_discovery"] = "unavailable"
        return
    payload["requestable_resources"] = visible


def _requestable_visible(
    caller: GatewayCaller,
    card: DelegatedCardView,
    resource: RequestableResource,
) -> bool:
    if resource.owner_subject != card.grantor_subject:
        return False
    if resource.identity_scope != card.identity_scope:
        return False
    if (
        resource.allowed_profile_ids
        and caller.caller_profile_id not in resource.allowed_profile_ids
    ):
        return False
    ceiling = caller.resource_ceiling
    return ceiling is None or any(
        resource_matches(pattern, resource.resource_id) for pattern in ceiling
    )


__all__ = ["ACCESS_DESCRIBE_SCHEMA", "access_describe_tool", "describe_access"]

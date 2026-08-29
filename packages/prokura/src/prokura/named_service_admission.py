# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Per-invocation named-service authority, independent of a host protocol.

The package evaluates card/catalog state and returns JSON-safe admission facts.
A host adapter maps those facts to its request, response, relay, and execution-
scope types.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from prokura.delegated_credentials.catalog.authorization import (
    CAPABILITY_NAMED_SERVICE_OPERATION,
    CapabilityRequest,
    CardProvenance,
    authorize_current_capability,
    card_boundary_denial,
    catalog_unavailable_denial,
)
from prokura.delegated_credentials.named_service_policy import (
    boundary_permits_operation,
    configured_named_service_operations,
)

ADMISSION_MODE_APPLICATION = "application"
ADMISSION_MODE_DELEGATED = "delegated"
DELEGATED_SELECTOR_AGENT = "agent_card"
DELEGATED_SELECTOR_BEARER = "bearer_card"

MANAGED_ADMISSION_STATE_ATTR = "named_service_admission_snapshot"
DELEGATED_CARD_BINDING_SCHEMA = "connection_hub.delegated_card_binding.v1"


def clean(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class ManagedNamedServiceAdmissionSnapshot:
    catalog: Any
    access_id: str
    client_id: str
    grantor_user_id: str
    delegate_identity: str
    expires_at: int
    resource: str
    request_resource: str
    outer_operation: str
    card_revision: int
    card_catalog_version: str
    named_services: Mapping[str, Any]
    named_services_present: bool
    account_scope: Mapping[str, Any]

    def selector(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "mode": ADMISSION_MODE_DELEGATED,
                "source": "managed_mcp.named_services",
                "delegated_kind": DELEGATED_SELECTOR_BEARER,
                "access_id": self.access_id,
                "client_id": self.client_id,
                "grantor_user_id": self.grantor_user_id,
                "delegate_identity": self.delegate_identity,
                "expires_at": self.expires_at,
            }.items()
            if value not in ("", 0, None)
        }


@dataclass(frozen=True)
class NamedServiceAdmissionEvaluation:
    allowed: bool
    denial: Mapping[str, Any] | None = None
    account_scope: Mapping[str, Any] = field(default_factory=dict)
    client_id: str = ""
    resource: str = ""
    audit: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        *,
        account_scope: Mapping[str, Any] | None = None,
        client_id: str = "",
        resource: str = "",
        audit: Mapping[str, Any] | None = None,
    ) -> "NamedServiceAdmissionEvaluation":
        return cls(
            allowed=True,
            account_scope=copy.deepcopy(dict(account_scope or {})),
            client_id=clean(client_id),
            resource=clean(resource),
            audit=dict(audit or {}),
        )

    @classmethod
    def deny(
        cls,
        denial: Mapping[str, Any],
        *,
        audit: Mapping[str, Any] | None = None,
    ) -> "NamedServiceAdmissionEvaluation":
        return cls(
            allowed=False,
            denial=copy.deepcopy(dict(denial or {})),
            audit=dict(audit or {}),
        )


def snapshot_from_grant(
    *,
    catalog: Any,
    grant_record: Mapping[str, Any],
    credential: Any,
    resource: str,
    request_resource: str,
    outer_operation: str = "",
) -> ManagedNamedServiceAdmissionSnapshot:
    attrs = getattr(credential, "attrs", None)
    attrs = dict(attrs) if isinstance(attrs, Mapping) else {}
    return ManagedNamedServiceAdmissionSnapshot(
        catalog=catalog,
        access_id=clean(grant_record.get("registry_access_id")),
        client_id=clean(grant_record.get("client_id") or attrs.get("client_id")),
        grantor_user_id=clean(
            grant_record.get("grantor_subject")
            or attrs.get("grantor_subject")
            or attrs.get("grantor_user_id")
        ),
        delegate_identity=clean(
            grant_record.get("delegate_subject")
            or getattr(credential, "subject", "")
        ),
        expires_at=int(grant_record.get("expires_at") or 0),
        resource=clean(resource),
        request_resource=clean(request_resource),
        outer_operation=clean(outer_operation),
        card_revision=int(grant_record.get("card_revision") or 0),
        card_catalog_version=clean(grant_record.get("catalog_version")),
        named_services=copy.deepcopy(
            dict(grant_record.get("named_services") or {})
        ),
        named_services_present=isinstance(
            grant_record.get("named_services"), Mapping
        ),
        account_scope=copy.deepcopy(
            dict(grant_record.get("account_scope") or {})
        ),
    )


def evaluate_managed_named_service(
    snapshot: ManagedNamedServiceAdmissionSnapshot,
    *,
    namespace: str,
    operation: str,
) -> NamedServiceAdmissionEvaluation:
    capability = CapabilityRequest(
        kind=CAPABILITY_NAMED_SERVICE_OPERATION,
        resource=snapshot.resource,
        request_resource=snapshot.request_resource,
        surface="named_service",
        outer_operation=snapshot.outer_operation,
        namespace=namespace,
        operation=operation,
    )
    provenance = CardProvenance(
        access_id=snapshot.access_id,
        card_revision=snapshot.card_revision,
        catalog_version=snapshot.card_catalog_version,
    )
    removed = authorize_current_capability(
        catalog=snapshot.catalog,
        provenance=provenance,
        request=capability,
    )
    if removed is not None:
        return NamedServiceAdmissionEvaluation.deny(removed)
    if not snapshot.named_services_present:
        return NamedServiceAdmissionEvaluation.deny(
            {
                "ok": False,
                "status": 503,
                "error": {
                    "code": "delegated_card_boundary_unavailable",
                    "message": (
                        "The delegated card does not carry a materialized "
                        "named-service boundary."
                    ),
                    "retryable": False,
                },
                "ret": {
                    "namespace": namespace,
                    "access_id": snapshot.access_id,
                },
            }
        )
    if not boundary_permits_operation(
        snapshot.named_services,
        namespace=namespace,
        operation=operation,
    ):
        return NamedServiceAdmissionEvaluation.deny(
            card_boundary_denial(provenance=provenance, request=capability)
        )
    return NamedServiceAdmissionEvaluation.allow(
        account_scope=snapshot.account_scope,
        client_id=snapshot.client_id,
        resource=snapshot.resource,
        audit={
            "mode": ADMISSION_MODE_DELEGATED,
            "source": "managed_mcp.named_services",
            "access_id": snapshot.access_id,
            "card_revision": snapshot.card_revision,
            "card_catalog_version": snapshot.card_catalog_version,
            "active_catalog_version": snapshot.catalog.version,
        },
    )


def hub_state_audit(
    state: Mapping[str, Any],
    *,
    selector: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "mode": ADMISSION_MODE_DELEGATED,
            "source": selector.get("source"),
            "access_id": state.get("access_id") or selector.get("access_id"),
            "card_revision": state.get("card_revision"),
            "card_catalog_version": state.get("card_catalog_version"),
            "active_catalog_version": state.get("active_catalog_version"),
        }.items()
        if value not in (None, "", 0)
    }


def hub_state_denial(
    state: Mapping[str, Any],
    *,
    selector: Mapping[str, Any],
) -> dict[str, Any]:
    unavailable = clean(state.get("unavailable"))
    if unavailable:
        return catalog_unavailable_denial(unavailable)
    for key in ("removed", "not_granted"):
        value = state.get(key)
        if isinstance(value, Mapping):
            return copy.deepcopy(dict(value))
    card_error = clean(state.get("card_error"))
    if card_error:
        return {
            "ok": False,
            "status": 403,
            "error": {
                "code": "delegated_card_not_active",
                "message": (
                    "The delegated access card is absent, expired, revoked, "
                    "or does not match this caller."
                ),
                "retryable": False,
                "details": {
                    "reason": card_error,
                    "access_id": state.get("access_id")
                    or selector.get("access_id"),
                },
            },
            "ret": {},
        }
    if not state.get("governed"):
        return {
            "ok": False,
            "status": 403,
            "error": {
                "code": "delegated_named_service_not_governed",
                "message": (
                    "The active delegated-service catalog does not publish "
                    "this named-service capability."
                ),
                "retryable": False,
                "details": {"retryable": False},
            },
            "ret": {},
        }
    return {
        "ok": False,
        "status": 403,
        "error": {
            "code": "delegated_capability_not_granted",
            "message": (
                "The delegated caller has not been granted this named-service "
                "capability."
            ),
            "retryable": False,
            "details": {
                "resource": state.get("resource") or "",
                "claims": list(
                    state.get("missing_claims") or state.get("claims") or []
                ),
                "client_id": selector.get("client_id") or "",
            },
        },
        "ret": {},
    }


def evaluate_resolved_hub_state(
    *,
    selector: Mapping[str, Any],
    state: Mapping[str, Any],
) -> NamedServiceAdmissionEvaluation:
    if not state.get("granted"):
        return NamedServiceAdmissionEvaluation.deny(
            hub_state_denial(state, selector=selector),
            audit=hub_state_audit(state, selector=selector),
        )
    return NamedServiceAdmissionEvaluation.allow(
        account_scope=(
            state.get("account_scope")
            if isinstance(state.get("account_scope"), Mapping)
            else {}
        ),
        client_id=clean(selector.get("client_id")),
        resource=clean(state.get("resource")),
        audit=hub_state_audit(state, selector=selector),
    )


def managed_catalog_operations(
    snapshot: ManagedNamedServiceAdmissionSnapshot,
) -> dict[str, set[str]]:
    resource_cfg = snapshot.catalog.resource_config(
        CapabilityRequest(
            kind=CAPABILITY_NAMED_SERVICE_OPERATION,
            resource=snapshot.resource,
            request_resource=snapshot.request_resource,
            surface="named_service",
            namespace="-",
            operation="-",
        )
    )
    named_services = getattr(resource_cfg, "named_services", None)
    if not isinstance(named_services, Mapping):
        return {}
    offered = configured_named_service_operations(named_services)
    card = configured_named_service_operations(snapshot.named_services)
    return {
        namespace: set(operations) & set(card.get(namespace) or ())
        for namespace, operations in offered.items()
        if set(operations) & set(card.get(namespace) or ())
    }


def _merge_dispatch_policy(
    fallback: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(fallback or {}))
    for key, value in dict(current or {}).items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_dispatch_policy(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def managed_dispatch_config(
    snapshot: ManagedNamedServiceAdmissionSnapshot,
) -> dict[str, Any]:
    resource_cfg = snapshot.catalog.resource_config(
        CapabilityRequest(
            kind=CAPABILITY_NAMED_SERVICE_OPERATION,
            resource=snapshot.resource,
            request_resource=snapshot.request_resource,
            surface="named_service",
            namespace="-",
            operation="-",
        )
    )
    active = getattr(resource_cfg, "named_services", None)
    active = active if isinstance(active, Mapping) else {}
    return _merge_dispatch_policy(snapshot.named_services, active)


def delegated_card_binding(
    snapshot: ManagedNamedServiceAdmissionSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None or not snapshot.access_id:
        return {}
    return {
        "schema": DELEGATED_CARD_BINDING_SCHEMA,
        "access_id": snapshot.access_id,
        "client_id": snapshot.client_id,
        "grantor_user_id": snapshot.grantor_user_id,
        "delegate_identity": snapshot.delegate_identity,
        "expires_at": snapshot.expires_at,
    }


def native_agent_selector(
    *,
    source_bundle_id: str,
    source_agent_id: str,
    client_id: str,
    grantor_user_id: str,
) -> dict[str, Any]:
    return {
        "mode": ADMISSION_MODE_DELEGATED,
        "source": "named_services.client_tool",
        "delegated_kind": DELEGATED_SELECTOR_AGENT,
        "client_id": clean(client_id),
        "grantor_user_id": clean(grantor_user_id),
        "source_bundle_id": clean(source_bundle_id),
        "source_agent_id": clean(source_agent_id),
    }


class NamedServiceAdmissionResolutionError(ValueError):
    pass


def validate_relay_selector(
    selector: Mapping[str, Any],
    *,
    actor: Mapping[str, Any],
) -> None:
    source_bundle = clean(actor.get("source_bundle_id"))
    mode = clean(selector.get("mode"))
    if mode == ADMISSION_MODE_APPLICATION:
        if not source_bundle:
            raise NamedServiceAdmissionResolutionError(
                "Application admission relay requires a trusted source bundle identity"
            )
        return

    actor_user_id = clean(actor.get("user_id") or actor.get("fingerprint"))
    grantor_user_id = clean(selector.get("grantor_user_id"))
    if grantor_user_id and actor_user_id != grantor_user_id:
        raise NamedServiceAdmissionResolutionError(
            "Relayed delegated selector does not match the carried grantor identity"
        )
    delegated_kind = clean(selector.get("delegated_kind"))
    if delegated_kind == DELEGATED_SELECTOR_AGENT:
        if source_bundle != clean(selector.get("source_bundle_id")) or clean(
            actor.get("source_agent_id")
        ) != clean(selector.get("source_agent_id")):
            raise NamedServiceAdmissionResolutionError(
                "Relayed agent-card selector does not match the carried caller"
            )
        return
    if delegated_kind != DELEGATED_SELECTOR_BEARER:
        raise NamedServiceAdmissionResolutionError(
            "Relayed delegated selector has an unsupported selector kind"
        )
    access_id = clean(selector.get("access_id"))
    if not access_id:
        raise NamedServiceAdmissionResolutionError(
            "Relayed bearer-card admission requires an exact access_id"
        )
    authority = actor.get("identity_authority")
    authority = authority if isinstance(authority, Mapping) else {}
    binding = authority.get("delegated_card_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    expected = {
        "access_id": access_id,
        "client_id": selector.get("client_id"),
        "grantor_user_id": grantor_user_id,
        "delegate_identity": selector.get("delegate_identity"),
    }
    if clean(binding.get("schema")) != DELEGATED_CARD_BINDING_SCHEMA or any(
        clean(binding.get(key)) != clean(expected_value)
        for key, expected_value in expected.items()
        if clean(expected_value)
    ):
        raise NamedServiceAdmissionResolutionError(
            "Relayed bearer-card selector does not match the authenticated session binding"
        )


__all__ = [
    "ADMISSION_MODE_APPLICATION",
    "ADMISSION_MODE_DELEGATED",
    "DELEGATED_CARD_BINDING_SCHEMA",
    "DELEGATED_SELECTOR_AGENT",
    "DELEGATED_SELECTOR_BEARER",
    "MANAGED_ADMISSION_STATE_ATTR",
    "ManagedNamedServiceAdmissionSnapshot",
    "NamedServiceAdmissionEvaluation",
    "NamedServiceAdmissionResolutionError",
    "clean",
    "delegated_card_binding",
    "evaluate_managed_named_service",
    "evaluate_resolved_hub_state",
    "hub_state_audit",
    "hub_state_denial",
    "managed_catalog_operations",
    "managed_dispatch_config",
    "native_agent_selector",
    "snapshot_from_grant",
    "validate_relay_selector",
]

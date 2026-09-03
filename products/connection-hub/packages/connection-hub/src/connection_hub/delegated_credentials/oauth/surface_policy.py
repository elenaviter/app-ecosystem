# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Transport-neutral delegated-card policy for managed REST and MCP surfaces.

The host authenticates the bearer, resolves its current durable card and active
catalog, then passes those facts here.  Connection Hub decides whether the concrete
resource, claim, and operation are still both offered by the catalog and held
by the card.  HTTP response objects and runtime-session projection remain host
concerns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.catalog.authorization import (
    CAPABILITY_OUTER_OPERATION,
    CAPABILITY_RESOURCE,
    CAPABILITY_RESOURCE_CLAIM,
    ActiveCatalogCapabilities,
    CapabilityRequest,
    CardProvenance,
    authorize_current_capability,
    card_boundary_denial,
)
from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
    resource_matches,
)

MANAGED_AUTH_MODE = "managed"
DELEGATED_PROXY_AUTH_MODE = "delegated_proxy"


def as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(
            item.strip()
            for item in value.replace(",", " ").split()
            if item.strip()
        )
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def auth_mode(auth: Mapping[str, Any] | None) -> str:
    if not isinstance(auth, Mapping):
        return ""
    return str(auth.get("mode") or "").strip().lower()


@dataclass(frozen=True)
class ManagedMcpToolPolicy:
    grants: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedMcpAuthPolicy:
    authority_id: str = ""
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    tool_policies: Mapping[str, ManagedMcpToolPolicy] | None = None
    selected_tool_grants: bool = True


@dataclass(frozen=True)
class DelegatedProxyAuthPolicy:
    """Authenticate a live delegated card for a proxy-owned inner resource.

    The hosted proxy URL is a transport entrance. The proxy resolves the
    concrete connector resource and operation from the live card itself.
    """

    authority_id: str = ""
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedRestOperationPolicy:
    grants: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedRestAuthPolicy:
    authority_id: str = ""
    grants: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    operation_policies: Mapping[str, ManagedRestOperationPolicy] | None = None
    selected_operation_grants: bool = False


def _parse_tool_policies(value: Any) -> dict[str, ManagedMcpToolPolicy]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, ManagedMcpToolPolicy] = {}
    for raw_name, raw_policy in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        data = raw_policy if isinstance(raw_policy, Mapping) else {}
        out[name] = ManagedMcpToolPolicy(
            grants=as_list(data.get("grants") or data.get("scopes")),
            roles=as_list(data.get("roles")),
            permissions=as_list(data.get("permissions")),
        )
    return out


def _parse_rest_operation_policies(
    value: Any,
) -> dict[str, ManagedRestOperationPolicy]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, ManagedRestOperationPolicy] = {}
    for raw_name, raw_policy in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        data = raw_policy if isinstance(raw_policy, Mapping) else {}
        out[name] = ManagedRestOperationPolicy(
            grants=as_list(
                data.get("grants")
                or data.get("scopes")
                or data.get("required_grants")
            ),
            roles=as_list(data.get("roles")),
            permissions=as_list(data.get("permissions")),
        )
    return out


def managed_mcp_auth_policy(
    auth: Mapping[str, Any] | None,
) -> ManagedMcpAuthPolicy | None:
    if auth_mode(auth) != MANAGED_AUTH_MODE:
        return None
    data = dict(auth or {})
    return ManagedMcpAuthPolicy(
        authority_id=str(
            data.get("authority_id") or data.get("authority") or ""
        ).strip(),
        roles=as_list(data.get("roles")),
        permissions=as_list(data.get("permissions")),
        tool_policies=_parse_tool_policies(
            data.get("tools") or data.get("tool_policies")
        ),
        selected_tool_grants=bool(data.get("selected_tool_grants", True)),
    )


def delegated_proxy_auth_policy(
    auth: Mapping[str, Any] | None,
) -> DelegatedProxyAuthPolicy | None:
    if auth_mode(auth) != DELEGATED_PROXY_AUTH_MODE:
        return None
    data = dict(auth or {})
    return DelegatedProxyAuthPolicy(
        authority_id=str(
            data.get("authority_id") or data.get("authority") or ""
        ).strip(),
        roles=as_list(data.get("roles")),
        permissions=as_list(data.get("permissions")),
    )


def managed_rest_auth_policy(
    auth: Mapping[str, Any] | None,
) -> ManagedRestAuthPolicy | None:
    if auth_mode(auth) != MANAGED_AUTH_MODE:
        return None
    data = dict(auth or {})
    operation_policies = _parse_rest_operation_policies(
        data.get("operations")
        or data.get("operation_policies")
        or data.get("tools")
        or data.get("tool_policies")
    )
    selected_operation_grants = data.get("selected_operation_grants")
    if selected_operation_grants is None:
        selected_operation_grants = data.get("selected_tool_grants")
    if selected_operation_grants is None:
        selected_operation_grants = bool(operation_policies)
    return ManagedRestAuthPolicy(
        authority_id=str(
            data.get("authority_id") or data.get("authority") or ""
        ).strip(),
        grants=as_list(
            data.get("grants")
            or data.get("scopes")
            or data.get("required_grants")
        ),
        roles=as_list(data.get("roles")),
        permissions=as_list(data.get("permissions")),
        operation_policies=operation_policies,
        selected_operation_grants=bool(selected_operation_grants),
    )


def decode_json_body(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def extract_mcp_tool_calls(body: bytes) -> list[tuple[Any, str]]:
    """Return the singular JSON-RPC ``tools/call`` accepted by the transport."""

    message = decode_json_body(body)
    if not isinstance(message, Mapping) or message.get("method") != "tools/call":
        return []
    params = message.get("params")
    if not isinstance(params, Mapping):
        return []
    name = str(params.get("name") or "").strip()
    return [(message.get("id"), name)] if name else []


@dataclass(frozen=True)
class SurfacePolicyDenial:
    reason: str
    status: int = 403
    error: str = "forbidden"
    description: str = ""
    payload: Mapping[str, Any] | None = None
    rpc_id: Any = None
    rpc_message: str = ""
    required_grants: frozenset[str] = frozenset()
    missing_grants: frozenset[str] = frozenset()
    available_grants: frozenset[str] = frozenset()

    @property
    def is_rpc(self) -> bool:
        return self.rpc_id is not None


@dataclass(frozen=True)
class SurfacePolicyDecision:
    allowed: bool
    denial: SurfacePolicyDenial | None = None
    matched_resource: str = ""
    stored_grants: frozenset[str] = frozenset()
    available_grants: frozenset[str] = frozenset()
    granted_operations: frozenset[str] = frozenset()

    @classmethod
    def allow(
        cls,
        *,
        matched_resource: str,
        stored_grants: Iterable[str] = (),
        available_grants: Iterable[str] = (),
        granted_operations: Iterable[str] = (),
    ) -> "SurfacePolicyDecision":
        return cls(
            allowed=True,
            matched_resource=matched_resource,
            stored_grants=frozenset(stored_grants),
            available_grants=frozenset(available_grants),
            granted_operations=frozenset(granted_operations),
        )

    @classmethod
    def deny(
        cls,
        denial: SurfacePolicyDenial,
        *,
        matched_resource: str = "",
        stored_grants: Iterable[str] = (),
        available_grants: Iterable[str] = (),
        granted_operations: Iterable[str] = (),
    ) -> "SurfacePolicyDecision":
        return cls(
            allowed=False,
            denial=denial,
            matched_resource=matched_resource,
            stored_grants=frozenset(stored_grants),
            available_grants=frozenset(available_grants),
            granted_operations=frozenset(granted_operations),
        )


def _resource_specificity(resource: str) -> tuple[int, int]:
    """Order matching card keys by how much they actually say: literal
    characters first, then overall length. The all-resource row ``*`` scores
    lowest by construction."""
    literal = len(resource.replace("*", "").replace("?", ""))
    return (literal, len(resource))


def _matched_resource(resources: Iterable[str], request_resource: str) -> str:
    """The card key this request is judged by: the MOST SPECIFIC matching one.

    A card routinely carries an all-resource row (``*``, holding only role
    grants) beside per-door rows that hold the operations. First-match
    selection let ``*`` steal the match, and every downstream check then read
    an empty operation list off it, refusing tools the specific row plainly
    granted. An exact key always wins; otherwise the most literal pattern
    does."""
    best = ""
    best_rank: tuple[int, int] | None = None
    for resource in resources:
        text = str(resource or "")
        if not resource_matches(text, request_resource):
            continue
        if text == request_resource:
            return text
        rank = _resource_specificity(text)
        if best_rank is None or rank > best_rank:
            best, best_rank = text, rank
    return best


def authorize_credential_boundary(
    *,
    authority_id: str,
    required_roles: Iterable[str],
    required_permissions: Iterable[str],
    user_roles: Iterable[str],
    user_permissions: Iterable[str],
    envelope: CredentialEnvelope,
    grant_record: Mapping[str, Any] | None,
    request_resource: str,
) -> SurfacePolicyDecision:
    """Check the caller and resource dimensions before catalog evaluation."""

    principal = authorize_principal_boundary(
        required_roles=required_roles,
        required_permissions=required_permissions,
        user_roles=user_roles,
        user_permissions=user_permissions,
    )
    if not principal.allowed:
        return principal

    if authority_id and envelope.issuer_authority_id != authority_id:
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="authority_mismatch",
                description="delegated credential authority mismatch",
            )
        )

    view = DelegatedCredentialView.from_envelope(envelope, grant_record)
    resources = view.resources
    if not resources:
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="credential_resource_missing",
                description="delegated credential resource is missing",
            )
        )
    matched_resource = _matched_resource(resources, request_resource)
    if not matched_resource:
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="resource_mismatch",
                description="delegated credential resource mismatch",
            )
        )

    operations = view.operations_for_resource(
        request_resource, matched_resource=matched_resource
    )
    stored_grants = view.grants_for_resource(request_resource)
    return SurfacePolicyDecision.allow(
        matched_resource=matched_resource,
        stored_grants=stored_grants,
        granted_operations=operations,
    )


def authorize_credential_identity_boundary(
    *,
    authority_id: str,
    required_roles: Iterable[str],
    required_permissions: Iterable[str],
    user_roles: Iterable[str],
    user_permissions: Iterable[str],
    envelope: CredentialEnvelope,
    grant_record: Mapping[str, Any] | None,
) -> SurfacePolicyDecision:
    """Authenticate a delegated caller and require a resource-bearing card.

    Used only by governed proxies whose own URL is not the protected resource.
    The inner proxy must subsequently select one of these exact card resources
    and enforce its grants and operations before dispatch.
    """

    principal = authorize_principal_boundary(
        required_roles=required_roles,
        required_permissions=required_permissions,
        user_roles=user_roles,
        user_permissions=user_permissions,
    )
    if not principal.allowed:
        return principal
    if authority_id and envelope.issuer_authority_id != authority_id:
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="authority_mismatch",
                description="delegated credential authority mismatch",
            )
        )
    view = DelegatedCredentialView.from_envelope(envelope, grant_record)
    if not view.resources:
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="credential_resource_missing",
                description="delegated credential resource is missing",
            )
        )
    return SurfacePolicyDecision.allow(matched_resource="")


def authorize_principal_boundary(
    *,
    required_roles: Iterable[str],
    required_permissions: Iterable[str],
    user_roles: Iterable[str],
    user_permissions: Iterable[str],
) -> SurfacePolicyDecision:
    """Check surface-level role and permission requirements."""

    roles = set(user_roles or ())
    permissions = set(user_permissions or ())
    required_role_set = set(required_roles or ())
    required_permission_set = set(required_permissions or ())
    if required_role_set and not roles.intersection(required_role_set):
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="missing_role",
                description="required role is missing",
            )
        )
    if required_permission_set and not permissions.issuperset(
        required_permission_set
    ):
        return SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="missing_permission",
                description="required permission is missing",
            )
        )
    return SurfacePolicyDecision.allow(matched_resource="")


def _provenance(grant_record: Mapping[str, Any] | None) -> CardProvenance:
    record = grant_record if isinstance(grant_record, Mapping) else {}
    try:
        revision = int(record.get("card_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    return CardProvenance(
        access_id=str(record.get("registry_access_id") or "").strip(),
        card_revision=revision,
        catalog_version=str(record.get("catalog_version") or "").strip(),
    )


def _capability_denial(
    *,
    catalog: ActiveCatalogCapabilities,
    grant_record: Mapping[str, Any] | None,
    kind: str,
    resource: str,
    request_resource: str,
    surface: str,
    claim: str = "",
    outer_operation: str = "",
) -> dict[str, Any] | None:
    return authorize_current_capability(
        catalog=catalog,
        provenance=_provenance(grant_record),
        request=CapabilityRequest(
            kind=kind,
            resource=resource,
            request_resource=request_resource,
            surface=surface,
            claim=claim,
            outer_operation=outer_operation,
        ),
    )


def _catalog_tool_policies(
    *,
    catalog: ActiveCatalogCapabilities,
    resource: str,
    request_resource: str,
    declared: Mapping[str, ManagedMcpToolPolicy] | None,
) -> dict[str, ManagedMcpToolPolicy]:
    row = catalog.resource_config(
        CapabilityRequest(
            kind=CAPABILITY_RESOURCE,
            resource=resource,
            request_resource=request_resource,
        )
    )
    declared = dict(declared or {})
    out: dict[str, ManagedMcpToolPolicy] = {}
    for tool in (getattr(row, "tools", None) or ()):
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        surface = declared.get(name)
        out[name] = ManagedMcpToolPolicy(
            grants=as_list(getattr(tool, "grants", ())),
            roles=getattr(surface, "roles", ()) or (),
            permissions=getattr(surface, "permissions", ()) or (),
        )
    if str(getattr(row, "resource", "") or "").strip().rstrip("/") == "*":
        for name, surface in declared.items():
            clean_name = str(name or "").strip()
            if not clean_name or clean_name in out:
                continue
            out[clean_name] = ManagedMcpToolPolicy(
                roles=getattr(surface, "roles", ()) or (),
                permissions=getattr(surface, "permissions", ()) or (),
            )
    return out


def _decision_with_state(
    boundary: SurfacePolicyDecision,
    *,
    allowed: bool,
    denial: SurfacePolicyDenial | None = None,
    available_grants: Iterable[str] = (),
) -> SurfacePolicyDecision:
    factory = SurfacePolicyDecision.allow if allowed else SurfacePolicyDecision.deny
    kwargs = {
        "matched_resource": boundary.matched_resource,
        "stored_grants": boundary.stored_grants,
        "available_grants": available_grants,
        "granted_operations": boundary.granted_operations,
    }
    if allowed:
        return factory(**kwargs)
    assert denial is not None
    return factory(denial, **kwargs)


def _operation_not_consented_denial(
    *,
    grant_record: Mapping[str, Any] | None,
    resource: str,
    request_resource: str,
    surface: str,
    operation: str,
    required_grants: Iterable[str] = (),
    available_grants: Iterable[str] = (),
    rpc_id: Any = None,
    structured_payload: bool = False,
) -> SurfacePolicyDenial:
    required = {str(item).strip() for item in required_grants if str(item).strip()}
    available = {
        str(item).strip() for item in available_grants if str(item).strip()
    }
    payload: Mapping[str, Any] | None = None
    if structured_payload:
        structured = card_boundary_denial(
            provenance=_provenance(grant_record),
            request=CapabilityRequest(
                kind=CAPABILITY_OUTER_OPERATION,
                resource=resource,
                request_resource=request_resource,
                surface=surface,
                outer_operation=operation,
            ),
        )
        ret = dict(structured.get("ret") or {})
        ret.update(
            {
                "required_grants": sorted(required),
                "missing_grants": sorted(required - available),
                "available_grants": sorted(available),
            }
        )
        structured["ret"] = ret
        payload = structured
    return SurfacePolicyDenial(
        reason="operation_not_consented",
        description=f"operation not consented for this connection: {operation}",
        payload=payload,
        rpc_id=rpc_id,
        required_grants=frozenset(required),
        missing_grants=frozenset(required - available),
        available_grants=frozenset(available),
    )


def authorize_mcp_capabilities(
    *,
    boundary: SurfacePolicyDecision,
    policy: ManagedMcpAuthPolicy,
    catalog: ActiveCatalogCapabilities,
    grant_record: Mapping[str, Any] | None,
    request_resource: str,
    user_roles: Iterable[str],
    user_permissions: Iterable[str],
    tool_calls: Iterable[tuple[Any, str]],
) -> SurfacePolicyDecision:
    if not boundary.allowed:
        return boundary
    resource = boundary.matched_resource
    path = {
        "catalog": catalog,
        "grant_record": grant_record,
        "resource": resource,
        "request_resource": request_resource,
        "surface": "mcp",
    }
    removed = _capability_denial(kind=CAPABILITY_RESOURCE, **path)
    if removed is not None:
        return _decision_with_state(
            boundary,
            allowed=False,
            denial=SurfacePolicyDenial(
                reason="capability_removed", payload=removed
            ),
        )

    available_grants = set(boundary.stored_grants) & set(
        catalog.resource_claims(
            CapabilityRequest(
                kind=CAPABILITY_RESOURCE_CLAIM,
                resource=resource,
                request_resource=request_resource,
            )
        )
    )
    calls = list(tool_calls)
    if not calls:
        return _decision_with_state(
            boundary, allowed=True, available_grants=available_grants
        )

    roles = set(user_roles or ())
    permissions = set(user_permissions or ())
    tool_policies = _catalog_tool_policies(
        catalog=catalog,
        resource=resource,
        request_resource=request_resource,
        declared=policy.tool_policies,
    )
    for rpc_id, tool_name in calls:
        removed = _capability_denial(
            kind=CAPABILITY_OUTER_OPERATION,
            outer_operation=tool_name,
            **path,
        )
        if removed is not None:
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=SurfacePolicyDenial(
                    reason="capability_removed",
                    payload=removed,
                    rpc_id=rpc_id,
                ),
            )

        tool_policy = tool_policies.get(tool_name)
        if tool_policies and tool_policy is None:
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=SurfacePolicyDenial(
                    reason="tool_not_allowed",
                    rpc_id=rpc_id,
                    rpc_message=f"tool not allowed by endpoint policy: {tool_name}",
                ),
            )
        if tool_policy is not None:
            if tool_policy.roles and not roles.intersection(tool_policy.roles):
                return _decision_with_state(
                    boundary,
                    allowed=False,
                    available_grants=available_grants,
                    denial=SurfacePolicyDenial(
                        reason="missing_tool_role",
                        rpc_id=rpc_id,
                        rpc_message=f"required role is missing for tool: {tool_name}",
                    ),
                )
            if tool_policy.permissions and not permissions.issuperset(
                tool_policy.permissions
            ):
                return _decision_with_state(
                    boundary,
                    allowed=False,
                    available_grants=available_grants,
                    denial=SurfacePolicyDenial(
                        reason="missing_tool_permission",
                        rpc_id=rpc_id,
                        rpc_message=(
                            f"required permission is missing for tool: {tool_name}"
                        ),
                    ),
                )
            removed_claims = sorted(
                set(tool_policy.grants)
                & (set(boundary.stored_grants) - available_grants)
            )
            if removed_claims:
                removed = _capability_denial(
                    kind=CAPABILITY_RESOURCE_CLAIM,
                    claim=removed_claims[0],
                    **path,
                )
                return _decision_with_state(
                    boundary,
                    allowed=False,
                    available_grants=available_grants,
                    denial=SurfacePolicyDenial(
                        reason="capability_removed",
                        payload=removed,
                        rpc_id=rpc_id,
                    ),
                )
        if policy.selected_tool_grants and tool_name not in boundary.granted_operations:
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=_operation_not_consented_denial(
                    grant_record=grant_record,
                    resource=resource,
                    request_resource=request_resource,
                    surface="mcp",
                    operation=tool_name,
                    required_grants=(
                        tool_policy.grants if tool_policy is not None else ()
                    ),
                    available_grants=available_grants,
                    rpc_id=rpc_id,
                    structured_payload=True,
                ),
            )

        if tool_policy is not None:
            missing = sorted(set(tool_policy.grants) - available_grants)
            if missing:
                payload = card_boundary_denial(
                    provenance=_provenance(grant_record),
                    request=CapabilityRequest(
                        kind=CAPABILITY_RESOURCE_CLAIM,
                        resource=resource,
                        request_resource=request_resource,
                        surface="mcp",
                        claim=missing[0],
                    ),
                )
                return _decision_with_state(
                    boundary,
                    allowed=False,
                    available_grants=available_grants,
                    denial=SurfacePolicyDenial(
                        reason="capability_not_granted",
                        payload=payload,
                        rpc_id=rpc_id,
                    ),
                )

    return _decision_with_state(
        boundary, allowed=True, available_grants=available_grants
    )


def authorize_rest_capabilities(
    *,
    boundary: SurfacePolicyDecision,
    policy: ManagedRestAuthPolicy,
    catalog: ActiveCatalogCapabilities,
    grant_record: Mapping[str, Any] | None,
    request_resource: str,
    operation: str,
    user_roles: Iterable[str],
    user_permissions: Iterable[str],
    operation_policies: Mapping[str, ManagedRestOperationPolicy] | None = None,
) -> SurfacePolicyDecision:
    if not boundary.allowed:
        return boundary
    resource = boundary.matched_resource
    operation_name = str(operation or "").strip()
    path = {
        "catalog": catalog,
        "grant_record": grant_record,
        "resource": resource,
        "request_resource": request_resource,
        "surface": "rest",
    }
    removed = _capability_denial(kind=CAPABILITY_RESOURCE, **path)
    if removed is None and operation_name:
        removed = _capability_denial(
            kind=CAPABILITY_OUTER_OPERATION,
            outer_operation=operation_name,
            **path,
        )
    if removed is not None:
        return _decision_with_state(
            boundary,
            allowed=False,
            denial=SurfacePolicyDenial(
                reason="capability_removed", payload=removed
            ),
        )

    available_grants = set(boundary.stored_grants) & set(
        catalog.resource_claims(
            CapabilityRequest(
                kind=CAPABILITY_RESOURCE_CLAIM,
                resource=resource,
                request_resource=request_resource,
            )
        )
    )

    def removed_claim(required: Iterable[str]) -> dict[str, Any] | None:
        claims = sorted(
            set(required or ())
            & (set(boundary.stored_grants) - available_grants)
        )
        if not claims:
            return None
        return _capability_denial(
            kind=CAPABILITY_RESOURCE_CLAIM,
            claim=claims[0],
            **path,
        )

    removed = removed_claim(policy.grants)
    if removed is not None:
        return _decision_with_state(
            boundary,
            allowed=False,
            available_grants=available_grants,
            denial=SurfacePolicyDenial(
                reason="capability_removed", payload=removed
            ),
        )
    operations = dict(operation_policies or policy.operation_policies or {})
    selected = policy.selected_operation_grants or bool(operations)
    operation_policy = operations.get(operation_name)
    if operations and operation_policy is None:
        return _decision_with_state(
            boundary,
            allowed=False,
            available_grants=available_grants,
            denial=SurfacePolicyDenial(
                reason="operation_not_allowed",
                description=(
                    f"operation not allowed by endpoint policy: {operation_name}"
                ),
            ),
        )

    roles = set(user_roles or ())
    permissions = set(user_permissions or ())
    if operation_policy is not None:
        if operation_policy.roles and not roles.intersection(operation_policy.roles):
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=SurfacePolicyDenial(
                    reason="missing_operation_role",
                    description=(
                        f"required role is missing for operation: {operation_name}"
                    ),
                ),
            )
        if operation_policy.permissions and not permissions.issuperset(
            operation_policy.permissions
        ):
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=SurfacePolicyDenial(
                    reason="missing_operation_permission",
                    description=(
                        f"required permission is missing for operation: {operation_name}"
                    ),
                ),
            )
        removed = removed_claim(operation_policy.grants)
        if removed is not None:
            return _decision_with_state(
                boundary,
                allowed=False,
                available_grants=available_grants,
                denial=SurfacePolicyDenial(
                    reason="capability_removed", payload=removed
                ),
            )
    if selected and operation_name not in boundary.granted_operations:
        required_grants = set(policy.grants)
        if operation_policy is not None:
            required_grants.update(operation_policy.grants)
        return _decision_with_state(
            boundary,
            allowed=False,
            available_grants=available_grants,
            denial=_operation_not_consented_denial(
                grant_record=grant_record,
                resource=resource,
                request_resource=request_resource,
                surface="rest",
                operation=operation_name,
                required_grants=required_grants,
                available_grants=available_grants,
            ),
        )

    if policy.grants and not available_grants.issuperset(policy.grants):
        return _decision_with_state(
            boundary,
            allowed=False,
            available_grants=available_grants,
            denial=SurfacePolicyDenial(
                reason="missing_grant",
                description="required delegated grant is missing",
            ),
        )

    if (
        operation_policy is not None
        and operation_policy.grants
        and not available_grants.issuperset(operation_policy.grants)
    ):
        return _decision_with_state(
            boundary,
            allowed=False,
            available_grants=available_grants,
            denial=SurfacePolicyDenial(
                reason="missing_operation_grant",
                description=(
                    "required delegated grant is missing for operation: "
                    f"{operation_name}"
                ),
            ),
        )

    return _decision_with_state(
        boundary, allowed=True, available_grants=available_grants
    )


__all__ = [
    "DELEGATED_PROXY_AUTH_MODE",
    "MANAGED_AUTH_MODE",
    "DelegatedProxyAuthPolicy",
    "ManagedMcpAuthPolicy",
    "ManagedMcpToolPolicy",
    "ManagedRestAuthPolicy",
    "ManagedRestOperationPolicy",
    "SurfacePolicyDecision",
    "SurfacePolicyDenial",
    "as_list",
    "auth_mode",
    "authorize_credential_boundary",
    "authorize_credential_identity_boundary",
    "authorize_mcp_capabilities",
    "authorize_principal_boundary",
    "authorize_rest_capabilities",
    "decode_json_body",
    "delegated_proxy_auth_policy",
    "extract_mcp_tool_calls",
    "managed_mcp_auth_policy",
    "managed_rest_auth_policy",
]

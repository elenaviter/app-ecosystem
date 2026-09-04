# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral records for the aggregate delegated MCP gateway."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ACCESS_DESCRIBE_TOOL = "connection_hub_access_describe"
DISCOVER_REQUESTABLE = "discover_requestable"

CARD_ACTIVE = "active"
CARD_REVOKED = "revoked"
RESOURCE_ACTIVE = "active"
RESOURCE_DISABLED = "disabled"
DESCRIPTOR_CURRENT = "current"
DESCRIPTOR_CHANGED = "changed"
DESCRIPTOR_MISSING = "missing"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,2047}$")
_RESOURCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._:@/*-]{0,2047}$")
_KIND = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "credential",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


class GatewayContractError(ValueError):
    """A gateway input cannot be trusted as a portable contract record."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DelegatedGatewayError(PermissionError):
    """A fixed, secret-safe denial returned by the portable gateway."""

    def __init__(
        self,
        reason: str,
        *,
        resource_id: str = "",
        operation: str = "",
        tool_name: str = "",
        access_id: str = "",
        card_revision: int = 0,
        invocation_id: str = "",
        retryable: bool = False,
        recovery: Mapping[str, Any] | None = None,
    ) -> None:
        code = str(reason or "").strip().lower()
        if not _ERROR_CODE.fullmatch(code):
            code = "gateway_denied"
        super().__init__(code)
        self.reason = code
        self.resource_id = _bounded_text(resource_id, 2048)
        self.operation = _bounded_text(operation, 256)
        self.tool_name = _bounded_text(tool_name, 128)
        self.access_id = _bounded_text(access_id, 256)
        self.card_revision = max(0, int(card_revision or 0))
        self.invocation_id = _bounded_text(invocation_id, 256)
        self.retryable = bool(retryable)
        self.recovery = public_mapping(recovery or {}, reason="recovery_not_public")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": "delegated_mcp_gateway_denied",
            "code": self.reason,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        for key, value in (
            ("resource_id", self.resource_id),
            ("operation", self.operation),
            ("tool_name", self.tool_name),
            ("access_id", self.access_id),
            ("invocation_id", self.invocation_id),
        ):
            if value:
                result[key] = value
        if self.card_revision:
            result["card_revision"] = self.card_revision
        if self.recovery:
            result["recovery"] = copy.deepcopy(self.recovery)
        return result


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else ""


def _required_identifier(value: Any, *, reason: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise GatewayContractError(reason)
    return text


def _resource_identifier(value: Any, *, reason: str) -> str:
    """Validate a concrete resource id or descriptor-owned glob selector."""

    text = str(value or "").strip()
    if not _RESOURCE_IDENTIFIER.fullmatch(text):
        raise GatewayContractError(reason)
    return text


def _kind(value: Any, *, reason: str) -> str:
    text = str(value or "").strip().lower()
    if not _KIND.fullmatch(text):
        raise GatewayContractError(reason)
    return text


def _operation(value: Any, *, reason: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(character) < 32 for character in text):
        raise GatewayContractError(reason)
    return text


def _digest(value: Any, *, reason: str, required: bool = True) -> str:
    text = str(value or "").strip().lower()
    if not text and not required:
        return ""
    if not _DIGEST.fullmatch(text):
        raise GatewayContractError(reason)
    return text


def _strings(values: Any, *, reason: str, limit: int = 256) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, (list, tuple, set, frozenset)):
        raise GatewayContractError(reason)
    result = {str(value or "").strip() for value in values}
    if "" in result or any(len(value) > limit for value in result):
        raise GatewayContractError(reason)
    return tuple(sorted(result))


def canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GatewayContractError("value_not_canonical_json") from exc
    return hashlib.sha256(payload).hexdigest()


def public_mapping(value: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    """Copy public metadata while rejecting credential-shaped fields."""

    if not isinstance(value, Mapping):
        raise GatewayContractError(reason)

    def _copy(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key or "").strip()
                normalized = key.lower().replace("-", "_")
                if not key or normalized in _FORBIDDEN_PUBLIC_KEYS:
                    raise GatewayContractError(reason)
                result[key] = _copy(raw_value)
            return result
        if isinstance(item, (list, tuple)):
            return [_copy(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, float) and not math.isfinite(item):
                raise GatewayContractError(reason)
            return item
        raise GatewayContractError(reason)

    copied = _copy(value)
    canonical_digest(copied)
    return copied


def json_copy(value: Any, *, reason: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise GatewayContractError(reason) from exc


@dataclass(frozen=True)
class AcceptedDescriptor:
    """Descriptor identity accepted by the card for one resource."""

    revision: str = ""
    digest: str = ""
    operation_digests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        revision = _bounded_text(self.revision, 256)
        digest = _digest(
            self.digest, reason="accepted_descriptor_digest_invalid", required=False
        )
        if not revision and not digest:
            raise GatewayContractError("accepted_descriptor_identity_missing")
        if not isinstance(self.operation_digests, Mapping):
            raise GatewayContractError("accepted_operation_digests_invalid")
        operations: dict[str, str] = {}
        for raw_operation, raw_digest in self.operation_digests.items():
            operation = _operation(raw_operation, reason="accepted_operation_invalid")
            operations[operation] = _digest(
                raw_digest, reason="accepted_operation_digest_invalid"
            )
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "operation_digests", operations)

    def operation_identity(self, operation: str) -> str:
        selected = _operation(operation, reason="operation_invalid")
        operation_digest = self.operation_digests.get(selected, "")
        if not operation_digest:
            raise GatewayContractError("accepted_operation_descriptor_missing")
        return canonical_digest(
            {
                "descriptor_digest": self.digest,
                "descriptor_revision": self.revision,
                "operation": selected,
                "operation_digest": operation_digest,
            }
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "digest": self.digest,
            "operation_digests": dict(self.operation_digests),
        }


@dataclass(frozen=True)
class InvocationPolicyView:
    mode: str
    state: str
    revision: int
    remaining: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"once", "always"}:
            raise GatewayContractError("invocation_policy_mode_invalid")
        if not _ERROR_CODE.fullmatch(str(self.state or "")):
            raise GatewayContractError("invocation_policy_state_invalid")
        if int(self.revision) < 1:
            raise GatewayContractError("invocation_policy_revision_invalid")
        if self.remaining is not None and int(self.remaining) < 0:
            raise GatewayContractError("invocation_policy_remaining_invalid")
        object.__setattr__(self, "revision", int(self.revision))
        if self.remaining is not None:
            object.__setattr__(self, "remaining", int(self.remaining))

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": self.mode,
            "state": self.state,
            "revision": self.revision,
        }
        if self.remaining is not None:
            result["remaining"] = self.remaining
        return result


@dataclass(frozen=True)
class RecoveryLink:
    code: str
    href: str

    def __post_init__(self) -> None:
        if not _ERROR_CODE.fullmatch(str(self.code or "")):
            raise GatewayContractError("recovery_code_invalid")
        href = str(self.href or "").strip()
        if not href or len(href) > 4096 or not href.startswith(("https://", "/")):
            raise GatewayContractError("recovery_href_invalid")
        object.__setattr__(self, "href", href)

    def to_public_dict(self) -> dict[str, str]:
        return {"code": self.code, "href": self.href}


@dataclass(frozen=True)
class DelegatedResourceEntry:
    resource_id: str
    kind: str
    display_label: str
    endpoint_relation: str
    grants: tuple[str, ...]
    operations: tuple[str, ...]
    accepted_descriptor: AcceptedDescriptor
    identity_scope: str
    provider_id: str = ""
    state: str = RESOURCE_ACTIVE
    unavailable_reason: str = ""
    invocation_policies: Mapping[str, InvocationPolicyView] = field(
        default_factory=dict
    )
    recovery: tuple[RecoveryLink, ...] = ()
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        resource_id = _resource_identifier(
            self.resource_id, reason="resource_id_invalid"
        )
        kind = _kind(self.kind, reason="resource_kind_invalid")
        label = _bounded_text(self.display_label, 256)
        relation = _bounded_text(self.endpoint_relation, 256)
        scope = _bounded_text(self.identity_scope, 256)
        provider_id = (
            _kind(self.provider_id, reason="resource_provider_id_invalid")
            if self.provider_id
            else ""
        )
        if not label or not relation or not scope:
            raise GatewayContractError("resource_identity_incomplete")
        if self.state not in {RESOURCE_ACTIVE, RESOURCE_DISABLED}:
            raise GatewayContractError("resource_state_invalid")
        unavailable = str(self.unavailable_reason or "").strip()
        if unavailable and not _ERROR_CODE.fullmatch(unavailable):
            raise GatewayContractError("resource_unavailable_reason_invalid")
        grants = _strings(self.grants, reason="resource_grants_invalid")
        operations = _strings(self.operations, reason="resource_operations_invalid")
        if not isinstance(self.accepted_descriptor, AcceptedDescriptor):
            raise GatewayContractError("accepted_descriptor_invalid")
        for operation in operations:
            self.accepted_descriptor.operation_identity(operation)
        if not isinstance(self.invocation_policies, Mapping):
            raise GatewayContractError("invocation_policies_invalid")
        policies: dict[str, InvocationPolicyView] = {}
        for raw_operation, policy in self.invocation_policies.items():
            operation = _operation(
                raw_operation, reason="invocation_policy_operation_invalid"
            )
            if operation not in operations:
                raise GatewayContractError("invocation_policy_operation_not_granted")
            if not isinstance(policy, InvocationPolicyView):
                raise GatewayContractError("invocation_policy_invalid")
            policies[operation] = policy
        recovery = tuple(self.recovery or ())
        if any(not isinstance(item, RecoveryLink) for item in recovery):
            raise GatewayContractError("recovery_link_invalid")
        if len({item.code for item in recovery}) != len(recovery):
            raise GatewayContractError("recovery_code_duplicate")
        object.__setattr__(self, "resource_id", resource_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "display_label", label)
        object.__setattr__(self, "endpoint_relation", relation)
        object.__setattr__(self, "identity_scope", scope)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "grants", grants)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "unavailable_reason", unavailable)
        object.__setattr__(self, "invocation_policies", policies)
        object.__setattr__(self, "recovery", recovery)
        object.__setattr__(
            self,
            "provider_metadata",
            public_mapping(
                self.provider_metadata or {}, reason="provider_metadata_not_public"
            ),
        )

    def recovery_for(self, code: str) -> dict[str, Any]:
        for item in self.recovery:
            if item.code == code:
                return item.to_public_dict()
        return {}


@dataclass(frozen=True)
class GatewayCaller:
    caller_type: str
    access_id: str
    caller_profile_id: str
    client_id: str = ""
    capabilities: tuple[str, ...] = ()
    resource_ceiling: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "caller_type", _kind(self.caller_type, reason="caller_type_invalid")
        )
        object.__setattr__(
            self,
            "access_id",
            _required_identifier(self.access_id, reason="access_id_invalid"),
        )
        object.__setattr__(
            self,
            "caller_profile_id",
            _required_identifier(
                self.caller_profile_id, reason="caller_profile_id_invalid"
            ),
        )
        client_id = _bounded_text(self.client_id, 512)
        object.__setattr__(self, "client_id", client_id)
        object.__setattr__(
            self,
            "capabilities",
            _strings(self.capabilities, reason="caller_capabilities_invalid"),
        )
        if self.resource_ceiling is not None:
            object.__setattr__(
                self,
                "resource_ceiling",
                _strings(
                    self.resource_ceiling,
                    reason="caller_resource_ceiling_invalid",
                    limit=2048,
                ),
            )


@dataclass(frozen=True)
class DelegatedCardView:
    caller_type: str
    caller_profile_id: str
    access_id: str
    card_revision: int
    status: str
    expires_at: int
    source: str
    identity_scope: str
    grantor_subject: str = field(repr=False)
    resources: tuple[DelegatedResourceEntry, ...] = ()
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        caller_type = _kind(self.caller_type, reason="card_caller_type_invalid")
        profile = _required_identifier(
            self.caller_profile_id, reason="card_caller_profile_id_invalid"
        )
        access_id = _required_identifier(
            self.access_id, reason="card_access_id_invalid"
        )
        if int(self.card_revision) < 1:
            raise GatewayContractError("card_revision_invalid")
        if int(self.expires_at) < 0:
            raise GatewayContractError("card_expiry_invalid")
        if self.status not in {CARD_ACTIVE, CARD_REVOKED}:
            raise GatewayContractError("card_status_invalid")
        source = _bounded_text(self.source, 128)
        scope = _bounded_text(self.identity_scope, 256)
        grantor = _bounded_text(self.grantor_subject, 1024)
        if not source or not scope or not grantor:
            raise GatewayContractError("card_identity_incomplete")
        resources = tuple(self.resources or ())
        if any(not isinstance(entry, DelegatedResourceEntry) for entry in resources):
            raise GatewayContractError("card_resource_invalid")
        ids = [entry.resource_id for entry in resources]
        if len(ids) != len(set(ids)):
            raise GatewayContractError("card_resource_duplicate")
        object.__setattr__(self, "caller_type", caller_type)
        object.__setattr__(self, "caller_profile_id", profile)
        object.__setattr__(self, "access_id", access_id)
        object.__setattr__(self, "card_revision", int(self.card_revision))
        object.__setattr__(self, "expires_at", int(self.expires_at))
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "identity_scope", scope)
        object.__setattr__(self, "grantor_subject", grantor)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(
            self,
            "capabilities",
            _strings(self.capabilities, reason="card_capabilities_invalid"),
        )

    def resource_map(self) -> dict[str, DelegatedResourceEntry]:
        return {entry.resource_id: entry for entry in self.resources}

    def permits_discovery(self, caller: GatewayCaller) -> bool:
        return DISCOVER_REQUESTABLE in set(self.capabilities) & set(caller.capabilities)


@dataclass(frozen=True)
class ProviderDescriptor:
    resource_id: str
    revision: str
    digest: str
    operation_digests: Mapping[str, str]
    state: str = DESCRIPTOR_CURRENT
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _resource_identifier(
                self.resource_id, reason="provider_resource_id_invalid"
            ),
        )
        revision = _bounded_text(self.revision, 256)
        digest = _digest(self.digest, reason="provider_descriptor_digest_invalid")
        if not revision:
            raise GatewayContractError("provider_descriptor_revision_missing")
        if self.state not in {
            DESCRIPTOR_CURRENT,
            DESCRIPTOR_CHANGED,
            DESCRIPTOR_MISSING,
            RESOURCE_DISABLED,
        }:
            raise GatewayContractError("provider_descriptor_state_invalid")
        unavailable = str(self.unavailable_reason or "").strip()
        if unavailable and not _ERROR_CODE.fullmatch(unavailable):
            raise GatewayContractError("provider_unavailable_reason_invalid")
        if not isinstance(self.operation_digests, Mapping):
            raise GatewayContractError("provider_operation_digests_invalid")
        operations: dict[str, str] = {}
        for raw_operation, raw_digest in self.operation_digests.items():
            operation = _operation(raw_operation, reason="provider_operation_invalid")
            operations[operation] = _digest(
                raw_digest, reason="provider_operation_digest_invalid"
            )
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "operation_digests", operations)
        object.__setattr__(self, "unavailable_reason", unavailable)

    @property
    def available(self) -> bool:
        return self.state == DESCRIPTOR_CURRENT and not self.unavailable_reason


@dataclass(frozen=True)
class ProviderTool:
    operation: str
    descriptor_digest: str
    title: str = ""
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        operation = _operation(self.operation, reason="provider_tool_invalid")
        digest = _digest(self.descriptor_digest, reason="provider_tool_digest_invalid")
        title = _bounded_text(self.title, 256) or operation
        description = _bounded_text(self.description, 4096)
        inputs = json_copy(
            self.input_schema or {}, reason="provider_input_schema_invalid"
        )
        outputs = (
            None
            if self.output_schema is None
            else json_copy(self.output_schema, reason="provider_output_schema_invalid")
        )
        if not isinstance(inputs, dict) or (
            outputs is not None and not isinstance(outputs, dict)
        ):
            raise GatewayContractError("provider_tool_schema_not_object")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "descriptor_digest", digest)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", inputs)
        object.__setattr__(self, "output_schema", outputs)


@dataclass(frozen=True)
class GatewayToolRoute:
    resource_id: str
    resource_kind: str
    operation: str
    accepted_descriptor_identity: str
    provider_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _resource_identifier(self.resource_id, reason="route_resource_id_invalid"),
        )
        object.__setattr__(
            self,
            "resource_kind",
            _kind(self.resource_kind, reason="route_resource_kind_invalid"),
        )
        object.__setattr__(
            self,
            "operation",
            _operation(self.operation, reason="route_operation_invalid"),
        )
        object.__setattr__(
            self,
            "accepted_descriptor_identity",
            _digest(
                self.accepted_descriptor_identity,
                reason="route_descriptor_identity_invalid",
            ),
        )
        if self.provider_id:
            object.__setattr__(
                self,
                "provider_id",
                _kind(self.provider_id, reason="route_provider_id_invalid"),
            )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            self.resource_id,
            self.operation,
            self.accepted_descriptor_identity,
        )


@dataclass(frozen=True)
class GatewayTool:
    name: str
    route: GatewayToolRoute | None
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name or len(name) > 128:
            raise GatewayContractError("gateway_tool_name_invalid")
        if self.route is not None and not isinstance(self.route, GatewayToolRoute):
            raise GatewayContractError("gateway_tool_route_invalid")
        title = _bounded_text(self.title, 256) or name
        description = _bounded_text(self.description, 4096)
        inputs = json_copy(self.input_schema, reason="gateway_input_schema_invalid")
        outputs = (
            None
            if self.output_schema is None
            else json_copy(self.output_schema, reason="gateway_output_schema_invalid")
        )
        if not isinstance(inputs, dict) or (
            outputs is not None and not isinstance(outputs, dict)
        ):
            raise GatewayContractError("gateway_tool_schema_not_object")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", inputs)
        object.__setattr__(self, "output_schema", outputs)

    def to_mcp_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": copy.deepcopy(dict(self.input_schema)),
        }
        if self.output_schema is not None:
            result["outputSchema"] = copy.deepcopy(dict(self.output_schema))
        if self.route is not None:
            result["_meta"] = {
                "connection_hub": {
                    "resource_id": self.route.resource_id,
                    "resource_kind": self.route.resource_kind,
                    "operation": self.route.operation,
                    "accepted_descriptor_identity": (
                        self.route.accepted_descriptor_identity
                    ),
                }
            }
        return result


@dataclass(frozen=True)
class ProviderCallAdmission:
    """Secret-free provider readiness result checked before policy use."""

    allowed: bool
    reason: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool) or not isinstance(self.retryable, bool):
            raise GatewayContractError("provider_admission_flags_invalid")
        reason = str(self.reason or "").strip().lower()
        if reason and not _ERROR_CODE.fullmatch(reason):
            raise GatewayContractError("provider_admission_reason_invalid")
        if self.allowed and reason:
            raise GatewayContractError("provider_admission_allowed_with_reason")
        if not self.allowed and not reason:
            raise GatewayContractError("provider_admission_reason_missing")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "retryable", bool(self.retryable))


@dataclass(frozen=True)
class ProviderCallResult:
    structured_content: Any
    content: tuple[Mapping[str, Any], ...] = ()
    is_error: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "structured_content",
            json_copy(self.structured_content, reason="provider_result_not_json"),
        )
        copied_content: list[Mapping[str, Any]] = []
        for block in tuple(self.content or ()):
            copied = json_copy(block, reason="provider_content_not_json")
            if not isinstance(copied, dict):
                raise GatewayContractError("provider_content_not_object")
            copied_content.append(copied)
        object.__setattr__(self, "content", tuple(copied_content))

    @classmethod
    def from_value(cls, value: Any) -> ProviderCallResult:
        return cls(structured_content=value)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "structured_content": json_copy(
                self.structured_content, reason="provider_result_not_json"
            ),
            "content": [copy.deepcopy(dict(block)) for block in self.content],
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class GatewayCallResult:
    result: ProviderCallResult
    access_id: str
    card_revision: int
    resource_id: str
    resource_kind: str
    operation: str
    tool_name: str
    invocation_id: str
    provider_id: str
    descriptor_revision: str
    descriptor_digest: str
    replay: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        payload = self.result.to_public_dict()
        payload["_meta"] = {
            "connection_hub": {
                "access_id": self.access_id,
                "card_revision": self.card_revision,
                "resource_id": self.resource_id,
                "resource_kind": self.resource_kind,
                "operation": self.operation,
                "tool_name": self.tool_name,
                "invocation_id": self.invocation_id,
                "provider_id": self.provider_id,
                "descriptor_revision": self.descriptor_revision,
                "descriptor_digest": self.descriptor_digest,
                "replay": self.replay,
            }
        }
        return payload


@dataclass(frozen=True)
class RequestableResource:
    resource_id: str
    kind: str
    display_label: str
    identity_scope: str
    owner_subject: str = field(repr=False)
    reason: str = "owner_delegable"
    allowed_profile_ids: tuple[str, ...] = ()
    recovery: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _resource_identifier(
                self.resource_id, reason="requestable_resource_id_invalid"
            ),
        )
        object.__setattr__(
            self, "kind", _kind(self.kind, reason="requestable_resource_kind_invalid")
        )
        label = _bounded_text(self.display_label, 256)
        scope = _bounded_text(self.identity_scope, 256)
        owner = _bounded_text(self.owner_subject, 1024)
        if not label or not scope or not owner:
            raise GatewayContractError("requestable_resource_identity_incomplete")
        if not _ERROR_CODE.fullmatch(str(self.reason or "")):
            raise GatewayContractError("requestable_reason_invalid")
        object.__setattr__(self, "display_label", label)
        object.__setattr__(self, "identity_scope", scope)
        object.__setattr__(self, "owner_subject", owner)
        object.__setattr__(
            self,
            "allowed_profile_ids",
            _strings(
                self.allowed_profile_ids,
                reason="requestable_profile_ids_invalid",
                limit=2048,
            ),
        )
        object.__setattr__(
            self,
            "recovery",
            public_mapping(
                self.recovery or {}, reason="requestable_recovery_not_public"
            ),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "display_label": self.display_label,
            "identity_scope": self.identity_scope,
            "reason": self.reason,
            "recovery": copy.deepcopy(dict(self.recovery)),
        }


__all__ = [
    "ACCESS_DESCRIBE_TOOL",
    "CARD_ACTIVE",
    "CARD_REVOKED",
    "DESCRIPTOR_CHANGED",
    "DESCRIPTOR_CURRENT",
    "DESCRIPTOR_MISSING",
    "DISCOVER_REQUESTABLE",
    "RESOURCE_ACTIVE",
    "RESOURCE_DISABLED",
    "AcceptedDescriptor",
    "DelegatedCardView",
    "DelegatedGatewayError",
    "DelegatedResourceEntry",
    "GatewayCallResult",
    "GatewayCaller",
    "GatewayContractError",
    "GatewayTool",
    "GatewayToolRoute",
    "InvocationPolicyView",
    "ProviderCallAdmission",
    "ProviderCallResult",
    "ProviderDescriptor",
    "ProviderTool",
    "RecoveryLink",
    "RequestableResource",
    "canonical_digest",
    "json_copy",
    "public_mapping",
]

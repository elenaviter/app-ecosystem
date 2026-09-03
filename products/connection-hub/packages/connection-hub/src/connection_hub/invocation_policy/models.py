# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Portable invocation-policy and idempotency records.

Delegated cards answer which operations a caller may use. These records answer
whether an already-granted operation is reusable or has one remaining use.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

INVOCATION_POLICY_SCHEMA = "connection_hub.invocation_policy.v1"
INVOCATION_RECORD_SCHEMA = "connection_hub.invocation_record.v1"
INVOCATION_POLICY_CHANGE_SCHEMA = "connection_hub.invocation_policy_change.v1"
REQUEST_BOUND_PERMIT_SCHEMA = "connection_hub.request_bound_permit.v1"

POLICY_ALWAYS = "always"
POLICY_ONCE = "once"
POLICY_MODES = frozenset({POLICY_ALWAYS, POLICY_ONCE})

POLICY_AVAILABLE = "available"
POLICY_CONSUMED = "consumed"
POLICY_STATES = frozenset({POLICY_AVAILABLE, POLICY_CONSUMED})

INVOCATION_RESERVED = "reserved"
INVOCATION_COMPLETED = "completed"
INVOCATION_STATES = frozenset({INVOCATION_RESERVED, INVOCATION_COMPLETED})

REQUEST_PERMIT_AVAILABLE = "available"
REQUEST_PERMIT_CONSUMED = "consumed"
REQUEST_PERMIT_STATES = frozenset(
    {REQUEST_PERMIT_AVAILABLE, REQUEST_PERMIT_CONSUMED}
)

POLICY_CHANGE_PREPARED = "prepared"
POLICY_CHANGE_COMMITTED = "committed"
POLICY_CHANGE_STATES = frozenset(
    {POLICY_CHANGE_PREPARED, POLICY_CHANGE_COMMITTED}
)

SURFACE_OUTER = "outer"

_ACCESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@~-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class InvocationPolicyRecordError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvocationPolicyRecordError("request_not_canonical_json") from exc
    return encoded.encode("utf-8")


def canonical_request_digest(value: Any) -> str:
    """Stable digest for the exact operation input represented by ``value``."""
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def validated_request_digest(value: Any) -> str:
    digest = _text(value).lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise InvocationPolicyRecordError("request_digest_invalid")
    return digest


def validated_invocation_id(value: Any) -> str:
    invocation_id = _text(value)
    if not invocation_id or len(invocation_id) > 256:
        raise InvocationPolicyRecordError("invocation_id_invalid")
    if any(ord(char) < 33 or ord(char) > 126 for char in invocation_id):
        raise InvocationPolicyRecordError("invocation_id_invalid")
    return invocation_id


@dataclass(frozen=True)
class InvocationAuthority:
    """The exact delegated authority path whose invocation policy is applied."""

    access_id: str
    resource: str
    surface: str
    operation: str
    provider_id: str = ""
    account_id: str = ""

    def __post_init__(self) -> None:
        access_id = _text(self.access_id)
        resource = _text(self.resource)
        surface = _text(self.surface).lower()
        operation = _text(self.operation)
        provider_id = _text(self.provider_id)
        account_id = _text(self.account_id)
        if not _ACCESS_ID_RE.fullmatch(access_id):
            raise InvocationPolicyRecordError("access_id_invalid")
        if not resource or len(resource) > 2048:
            raise InvocationPolicyRecordError("resource_invalid")
        if not _TOKEN_RE.fullmatch(surface):
            raise InvocationPolicyRecordError("surface_invalid")
        if not operation or len(operation) > 512:
            raise InvocationPolicyRecordError("operation_invalid")
        if bool(provider_id) != bool(account_id):
            raise InvocationPolicyRecordError("account_binding_incomplete")
        if provider_id and not _TOKEN_RE.fullmatch(provider_id):
            raise InvocationPolicyRecordError("provider_id_invalid")
        if account_id and len(account_id) > 512:
            raise InvocationPolicyRecordError("account_id_invalid")
        object.__setattr__(self, "access_id", access_id)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "account_id", account_id)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InvocationAuthority":
        account = value.get("account")
        account = account if isinstance(account, Mapping) else {}
        return cls(
            access_id=value.get("access_id", ""),
            resource=value.get("resource", ""),
            surface=value.get("surface", ""),
            operation=value.get("operation", ""),
            provider_id=account.get("provider_id", ""),
            account_id=account.get("account_id", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "access_id": self.access_id,
            "resource": self.resource,
            "surface": self.surface,
            "operation": self.operation,
        }
        if self.provider_id:
            out["account"] = {
                "provider_id": self.provider_id,
                "account_id": self.account_id,
            }
        return out

    @property
    def key(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class InvocationPolicy:
    authority: InvocationAuthority
    mode: str
    revision: int
    state: str = POLICY_AVAILABLE
    consumed_invocation_id: str = ""
    consumed_request_digest: str = ""
    consumed_at: int = 0
    updated_at: int = 0

    def __post_init__(self) -> None:
        mode = _text(self.mode).lower()
        state = _text(self.state).lower()
        if mode not in POLICY_MODES:
            raise InvocationPolicyRecordError("policy_mode_invalid")
        if state not in POLICY_STATES:
            raise InvocationPolicyRecordError("policy_state_invalid")
        if int(self.revision) < 1:
            raise InvocationPolicyRecordError("policy_revision_invalid")
        if mode == POLICY_ALWAYS and state != POLICY_AVAILABLE:
            raise InvocationPolicyRecordError("reusable_policy_state_invalid")
        if state == POLICY_CONSUMED:
            validated_invocation_id(self.consumed_invocation_id)
            validated_request_digest(self.consumed_request_digest)
            if int(self.consumed_at) < 1:
                raise InvocationPolicyRecordError("consumed_at_invalid")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "revision", int(self.revision))
        object.__setattr__(self, "consumed_at", int(self.consumed_at))
        object.__setattr__(self, "updated_at", int(self.updated_at))

    @property
    def policy_id(self) -> str:
        return f"invpol_{self.authority.key[:24]}"

    @property
    def remaining(self) -> int | None:
        if self.mode == POLICY_ALWAYS:
            return None
        return 0 if self.state == POLICY_CONSUMED else 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InvocationPolicy":
        if _text(value.get("schema")) != INVOCATION_POLICY_SCHEMA:
            raise InvocationPolicyRecordError("policy_schema_mismatch")
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise InvocationPolicyRecordError("policy_authority_invalid")
        return cls(
            authority=InvocationAuthority.from_mapping(authority),
            mode=value.get("mode", ""),
            revision=int(value.get("revision") or 0),
            state=value.get("state", POLICY_AVAILABLE),
            consumed_invocation_id=value.get("consumed_invocation_id", ""),
            consumed_request_digest=value.get("consumed_request_digest", ""),
            consumed_at=int(value.get("consumed_at") or 0),
            updated_at=int(value.get("updated_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": INVOCATION_POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "authority": self.authority.to_dict(),
            "mode": self.mode,
            "revision": self.revision,
            "state": self.state,
            "remaining": self.remaining,
            "updated_at": self.updated_at,
        }
        if self.state == POLICY_CONSUMED:
            out.update(
                {
                    "consumed_invocation_id": self.consumed_invocation_id,
                    "consumed_request_digest": self.consumed_request_digest,
                    "consumed_at": self.consumed_at,
                }
            )
        return out

    def to_public_dict(self) -> dict[str, Any]:
        out = self.to_dict()
        out.pop("consumed_request_digest", None)
        return out


@dataclass(frozen=True)
class InvocationPolicyChange:
    """Fail-closed bridge across a card mutation and its policy mutation."""

    authority: InvocationAuthority
    change_id: str
    mode: str
    state: str
    expected_policy_revision: int
    policy_revision: int = 0
    prepared_at: int = 0
    committed_at: int = 0

    def __post_init__(self) -> None:
        change_id = validated_invocation_id(self.change_id)
        mode = _text(self.mode).lower()
        state = _text(self.state).lower()
        expected_revision = int(self.expected_policy_revision)
        policy_revision = int(self.policy_revision)
        prepared_at = int(self.prepared_at)
        committed_at = int(self.committed_at)
        if mode not in POLICY_MODES:
            raise InvocationPolicyRecordError("policy_mode_invalid")
        if state not in POLICY_CHANGE_STATES:
            raise InvocationPolicyRecordError("policy_change_state_invalid")
        if expected_revision < 0 or policy_revision < 0:
            raise InvocationPolicyRecordError("policy_revision_invalid")
        if prepared_at < 1:
            raise InvocationPolicyRecordError("policy_change_prepared_at_invalid")
        if state == POLICY_CHANGE_PREPARED:
            if policy_revision or committed_at:
                raise InvocationPolicyRecordError("policy_change_not_committed")
        elif policy_revision != expected_revision + 1 or committed_at < prepared_at:
            raise InvocationPolicyRecordError("policy_change_commit_invalid")
        object.__setattr__(self, "change_id", change_id)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "expected_policy_revision", expected_revision)
        object.__setattr__(self, "policy_revision", policy_revision)
        object.__setattr__(self, "prepared_at", prepared_at)
        object.__setattr__(self, "committed_at", committed_at)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InvocationPolicyChange":
        if _text(value.get("schema")) != INVOCATION_POLICY_CHANGE_SCHEMA:
            raise InvocationPolicyRecordError("policy_change_schema_mismatch")
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise InvocationPolicyRecordError("policy_change_authority_invalid")
        return cls(
            authority=InvocationAuthority.from_mapping(authority),
            change_id=value.get("change_id", ""),
            mode=value.get("mode", ""),
            state=value.get("state", ""),
            expected_policy_revision=int(
                value.get("expected_policy_revision") or 0
            ),
            policy_revision=int(value.get("policy_revision") or 0),
            prepared_at=int(value.get("prepared_at") or 0),
            committed_at=int(value.get("committed_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INVOCATION_POLICY_CHANGE_SCHEMA,
            "authority": self.authority.to_dict(),
            "change_id": self.change_id,
            "mode": self.mode,
            "state": self.state,
            "expected_policy_revision": self.expected_policy_revision,
            "policy_revision": self.policy_revision,
            "prepared_at": self.prepared_at,
            "committed_at": self.committed_at,
        }


@dataclass(frozen=True)
class RequestBoundPermit:
    """One browser-approved invocation under an otherwise one-use policy."""

    authority: InvocationAuthority
    invocation_id: str
    request_digest: str
    card_revision: int
    authority_revision: str
    policy_revision: int
    revision: int
    state: str = REQUEST_PERMIT_AVAILABLE
    issued_at: int = 0
    expires_at: int = 0
    consumed_at: int = 0

    def __post_init__(self) -> None:
        invocation_id = validated_invocation_id(self.invocation_id)
        request_digest = validated_request_digest(self.request_digest)
        authority_revision = _text(self.authority_revision)
        state = _text(self.state).lower()
        card_revision = int(self.card_revision)
        policy_revision = int(self.policy_revision)
        revision = int(self.revision)
        issued_at = int(self.issued_at)
        expires_at = int(self.expires_at)
        consumed_at = int(self.consumed_at)
        if state not in REQUEST_PERMIT_STATES:
            raise InvocationPolicyRecordError("request_permit_state_invalid")
        if card_revision < 1:
            raise InvocationPolicyRecordError("request_permit_card_revision_invalid")
        if not authority_revision:
            raise InvocationPolicyRecordError("request_permit_authority_revision_missing")
        if policy_revision < 1 or revision < 1:
            raise InvocationPolicyRecordError("request_permit_revision_invalid")
        if issued_at < 1 or expires_at <= issued_at:
            raise InvocationPolicyRecordError("request_permit_expiry_invalid")
        if state == REQUEST_PERMIT_AVAILABLE and consumed_at:
            raise InvocationPolicyRecordError("request_permit_not_consumed")
        if state == REQUEST_PERMIT_CONSUMED and consumed_at < issued_at:
            raise InvocationPolicyRecordError("request_permit_consumption_invalid")
        object.__setattr__(self, "invocation_id", invocation_id)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "authority_revision", authority_revision)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "card_revision", card_revision)
        object.__setattr__(self, "policy_revision", policy_revision)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "consumed_at", consumed_at)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequestBoundPermit":
        if _text(value.get("schema")) != REQUEST_BOUND_PERMIT_SCHEMA:
            raise InvocationPolicyRecordError("request_permit_schema_mismatch")
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise InvocationPolicyRecordError("request_permit_authority_invalid")
        return cls(
            authority=InvocationAuthority.from_mapping(authority),
            invocation_id=value.get("invocation_id", ""),
            request_digest=value.get("request_digest", ""),
            card_revision=int(value.get("card_revision") or 0),
            authority_revision=value.get("authority_revision", ""),
            policy_revision=int(value.get("policy_revision") or 0),
            revision=int(value.get("revision") or 0),
            state=value.get("state", ""),
            issued_at=int(value.get("issued_at") or 0),
            expires_at=int(value.get("expires_at") or 0),
            consumed_at=int(value.get("consumed_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_BOUND_PERMIT_SCHEMA,
            "authority": self.authority.to_dict(),
            "invocation_id": self.invocation_id,
            "request_digest": self.request_digest,
            "card_revision": self.card_revision,
            "authority_revision": self.authority_revision,
            "policy_revision": self.policy_revision,
            "revision": self.revision,
            "state": self.state,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        out = self.to_dict()
        out.pop("request_digest", None)
        return out


@dataclass(frozen=True)
class InvocationRecord:
    authority: InvocationAuthority
    invocation_id: str
    request_digest: str
    policy_id: str
    policy_revision: int
    policy_mode: str
    state: str
    card_revision: int = 0
    authority_revision: str = ""
    request_permit_revision: int = 0
    result: Any = None
    result_is_error: bool = False
    created_at: int = 0
    completed_at: int = 0

    def __post_init__(self) -> None:
        invocation_id = validated_invocation_id(self.invocation_id)
        request_digest = validated_request_digest(self.request_digest)
        state = _text(self.state).lower()
        mode = _text(self.policy_mode).lower()
        if state not in INVOCATION_STATES:
            raise InvocationPolicyRecordError("invocation_state_invalid")
        if mode not in POLICY_MODES:
            raise InvocationPolicyRecordError("policy_mode_invalid")
        if state == INVOCATION_COMPLETED and int(self.completed_at) < 1:
            raise InvocationPolicyRecordError("invocation_completed_at_invalid")
        _canonical_json(self.result)
        object.__setattr__(self, "invocation_id", invocation_id)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "policy_mode", mode)
        object.__setattr__(self, "policy_revision", int(self.policy_revision))
        object.__setattr__(self, "card_revision", max(0, int(self.card_revision)))
        object.__setattr__(
            self,
            "request_permit_revision",
            max(0, int(self.request_permit_revision)),
        )
        object.__setattr__(self, "created_at", int(self.created_at))
        object.__setattr__(self, "completed_at", int(self.completed_at))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InvocationRecord":
        if _text(value.get("schema")) != INVOCATION_RECORD_SCHEMA:
            raise InvocationPolicyRecordError("invocation_schema_mismatch")
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise InvocationPolicyRecordError("invocation_authority_invalid")
        return cls(
            authority=InvocationAuthority.from_mapping(authority),
            invocation_id=value.get("invocation_id", ""),
            request_digest=value.get("request_digest", ""),
            policy_id=value.get("policy_id", ""),
            policy_revision=int(value.get("policy_revision") or 0),
            policy_mode=value.get("policy_mode", ""),
            state=value.get("state", ""),
            card_revision=int(value.get("card_revision") or 0),
            authority_revision=_text(value.get("authority_revision")),
            request_permit_revision=int(value.get("request_permit_revision") or 0),
            result=value.get("result"),
            result_is_error=bool(value.get("result_is_error", False)),
            created_at=int(value.get("created_at") or 0),
            completed_at=int(value.get("completed_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INVOCATION_RECORD_SCHEMA,
            "authority": self.authority.to_dict(),
            "invocation_id": self.invocation_id,
            "request_digest": self.request_digest,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "policy_mode": self.policy_mode,
            "state": self.state,
            "card_revision": self.card_revision,
            "authority_revision": self.authority_revision,
            "request_permit_revision": self.request_permit_revision,
            "result": self.result,
            "result_is_error": self.result_is_error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class InvocationDecision:
    allowed: bool
    reason: str
    dispatch: bool
    replay: bool = False
    retryable: bool = False
    policy: InvocationPolicy | None = None
    invocation: InvocationRecord | None = None
    request_permit: RequestBoundPermit | None = None

    @property
    def result(self) -> Any:
        return self.invocation.result if self.invocation is not None else None

    @property
    def result_is_error(self) -> bool:
        return bool(self.invocation and self.invocation.result_is_error)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "allowed": self.allowed,
            "reason": self.reason,
            "dispatch": self.dispatch,
            "replay": self.replay,
            "retryable": self.retryable,
        }
        if self.policy is not None:
            out["policy"] = self.policy.to_public_dict()
        if self.invocation is not None:
            out["invocation"] = {
                "invocation_id": self.invocation.invocation_id,
                "state": self.invocation.state,
                "policy_revision": self.invocation.policy_revision,
                "card_revision": self.invocation.card_revision,
                "authority_revision": self.invocation.authority_revision,
                "request_permit_revision": self.invocation.request_permit_revision,
            }
        if self.request_permit is not None:
            out["request_permit"] = self.request_permit.to_public_dict()
        return out


__all__ = [
    "INVOCATION_COMPLETED",
    "INVOCATION_POLICY_CHANGE_SCHEMA",
    "INVOCATION_POLICY_SCHEMA",
    "INVOCATION_RECORD_SCHEMA",
    "INVOCATION_RESERVED",
    "POLICY_ALWAYS",
    "POLICY_AVAILABLE",
    "POLICY_CONSUMED",
    "POLICY_CHANGE_COMMITTED",
    "POLICY_CHANGE_PREPARED",
    "POLICY_ONCE",
    "REQUEST_BOUND_PERMIT_SCHEMA",
    "REQUEST_PERMIT_AVAILABLE",
    "REQUEST_PERMIT_CONSUMED",
    "SURFACE_OUTER",
    "InvocationAuthority",
    "InvocationDecision",
    "InvocationPolicy",
    "InvocationPolicyChange",
    "InvocationPolicyRecordError",
    "InvocationRecord",
    "RequestBoundPermit",
    "canonical_request_digest",
    "validated_invocation_id",
    "validated_request_digest",
]

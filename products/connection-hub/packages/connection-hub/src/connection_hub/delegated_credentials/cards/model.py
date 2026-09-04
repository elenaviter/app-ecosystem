# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Delegated-card authority, credential handles, and durable revision identity.

Authority is reconstructable and non-secret: it is what a durable revision
stores and what a Redis projection restores. Credential handles are live,
bounded, and never part of durable history.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from connection_hub.delegated_credentials.catalog.descriptors import (
    ResourceAcceptance,
    ResourceAcceptanceError,
    parse_resource_acceptance,
)
from connection_hub.delegated_credentials.catalog.models import (
    utc_stamp,
    utc_timestamp,
)
from connection_hub.delegated_credentials.named_service_policy import (
    as_string_list,
    clean_text,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_operations,
    operation_union,
    project_legacy_operations,
)

CARD_AUTHORITY_SCHEMA_V1 = "connection_hub.delegated_card_authority.v1"
CARD_AUTHORITY_SCHEMA_V2 = "connection_hub.delegated_card_authority.v2"
# v3 adds per-resource accepted descriptor state and card provenance. Both are
# optional on read, so v2 (and projected v1) revisions load unchanged; the next
# successful write persists v3.
CARD_AUTHORITY_SCHEMA = "connection_hub.delegated_card_authority.v3"
CARD_AUTHORITY_SCHEMAS = (
    CARD_AUTHORITY_SCHEMA_V1,
    CARD_AUTHORITY_SCHEMA_V2,
    CARD_AUTHORITY_SCHEMA,
)
CARD_POINTER_SCHEMA = "connection_hub.delegated_card_current.v1"

CARD_STATE_ACTIVE = "active"
CARD_STATE_REVOKED = "revoked"

NAMED_SERVICE_OPERATIONS_ALL = "*"

SELECTION_ALL = "all"
SELECTION_NONE = "none"
SELECTION_EXACT = "exact"
SELECTION_UNKNOWN = "unknown"

_REVISION_HASH_CHARS = 12


class CardRecordError(ValueError):
    """A card payload could not be trusted as authorization state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def card_authority_payload_hash(value: Mapping[str, Any]) -> str:
    """Hash the exact durable payload before any in-memory migration."""
    if not isinstance(value, Mapping):
        raise CardRecordError("record_not_object")
    canonical = json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalized_namespace(value: Any) -> str:
    return clean_text(value).lower().rstrip(":")


@dataclass(frozen=True)
class NamedServiceSelection:
    """The card's named-service authority as one of four states.

    ``unknown`` is a legacy-record condition only; newly written cards carry
    ``all``, ``none``, or ``exact``.
    """

    kind: str
    operations: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def all(cls) -> "NamedServiceSelection":
        return cls(kind=SELECTION_ALL)

    @classmethod
    def none(cls) -> "NamedServiceSelection":
        return cls(kind=SELECTION_NONE)

    @classmethod
    def unknown(cls) -> "NamedServiceSelection":
        return cls(kind=SELECTION_UNKNOWN)

    @classmethod
    def exact(cls, operations: Mapping[str, Any]) -> "NamedServiceSelection":
        """An exact resource/namespace/operation map. An empty map is ``none``."""
        normalized: dict[str, dict[str, tuple[str, ...]]] = {}
        for resource, namespaces in dict(operations or {}).items():
            resource_value = clean_text(resource)
            if not resource_value:
                continue
            if not isinstance(namespaces, Mapping):
                raise CardRecordError("named_service_operations_resource_invalid")
            per_namespace: dict[str, tuple[str, ...]] = {}
            for namespace, values in namespaces.items():
                namespace_value = _normalized_namespace(namespace)
                if not namespace_value:
                    continue
                per_namespace[namespace_value] = tuple(as_string_list(values))
            normalized[resource_value] = per_namespace
        if not normalized:
            return cls.none()
        return cls(kind=SELECTION_EXACT, operations=normalized)

    @classmethod
    def from_stored(cls, value: Any, *, present: bool) -> "NamedServiceSelection":
        """Parse the stored field.

        ``present`` is the raw field's presence in the payload; it separates an
        explicitly empty policy from a legacy record.
        """
        if not present or value is None:
            return cls.unknown()
        if isinstance(value, str):
            if clean_text(value) != NAMED_SERVICE_OPERATIONS_ALL:
                raise CardRecordError("named_service_operations_invalid")
            return cls.all()
        if isinstance(value, Mapping):
            if not value:
                return cls.none()
            return cls.exact(value)
        raise CardRecordError("named_service_operations_invalid")

    def to_stored(self) -> str | dict[str, dict[str, list[str]]] | None:
        """The durable representation. ``unknown`` writes nothing."""
        if self.kind == SELECTION_ALL:
            return NAMED_SERVICE_OPERATIONS_ALL
        if self.kind == SELECTION_NONE:
            return {}
        if self.kind == SELECTION_EXACT:
            return {
                resource: {namespace: list(values) for namespace, values in namespaces.items()}
                for resource, namespaces in self.operations.items()
            }
        return None

    def union(self, other: "NamedServiceSelection") -> "NamedServiceSelection":
        """Widest of the two selections.

        A full or legacy-unknown side absorbs the other; two exact selections
        merge per resource and namespace.
        """
        if self.is_all or self.is_unknown:
            return self
        if other.is_all or other.is_unknown:
            return other
        if self.is_none:
            return other
        if other.is_none:
            return self
        merged: dict[str, dict[str, list[str]]] = {
            resource: {namespace: list(values) for namespace, values in namespaces.items()}
            for resource, namespaces in self.operations.items()
        }
        for resource, namespaces in other.operations.items():
            target = merged.setdefault(resource, {})
            for namespace, values in namespaces.items():
                held = target.setdefault(namespace, [])
                for value in values:
                    if value not in held:
                        held.append(value)
        return NamedServiceSelection.exact(merged)

    @property
    def is_all(self) -> bool:
        return self.kind == SELECTION_ALL

    @property
    def is_none(self) -> bool:
        return self.kind == SELECTION_NONE

    @property
    def is_exact(self) -> bool:
        return self.kind == SELECTION_EXACT

    @property
    def is_unknown(self) -> bool:
        return self.kind == SELECTION_UNKNOWN


@dataclass(frozen=True)
class CardAuthority:
    """One card's complete non-secret authorization decision."""

    access_id: str
    client_id: str
    grantor_subject: str
    delegate_subject: str
    source: str
    label: str = ""
    card_revision: int = 0
    catalog_version: str = ""
    state: str = CARD_STATE_ACTIVE
    operations: tuple[str, ...] = ()
    resource_grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    resource_operations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    named_service_operations: NamedServiceSelection = field(
        default_factory=NamedServiceSelection.unknown
    )
    named_services: Mapping[str, Any] = field(default_factory=dict)
    account_scope: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    identity_scope: str = ""
    created_at: int = 0
    expires_at: int = 0
    last_issued_at: int = 0
    last_four: str = ""
    # Per resource: the descriptor authority (catalog row or connector), the
    # revision and digest accepted at the last save, and one digest per
    # offered operation. Drift is judged against this, resource by resource.
    # Absent for revisions written before v3.
    resource_acceptance: Mapping[str, ResourceAcceptance] = field(default_factory=dict)
    # Non-secret lineage: which records were merged into this card and when.
    # Written by the resident-profile migration; empty otherwise.
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "CardAuthority":
        if not isinstance(value, Mapping):
            raise CardRecordError("record_not_object")
        schema = str(value.get("schema") or "").strip()
        if schema not in CARD_AUTHORITY_SCHEMAS:
            raise CardRecordError("schema_mismatch")
        try:
            resource_acceptance = parse_resource_acceptance(value.get("resource_acceptance"))
        except ResourceAcceptanceError as exc:
            raise CardRecordError("resource_acceptance_invalid") from exc
        provenance = value.get("provenance", {})
        if provenance is None:
            provenance = {}
        if not isinstance(provenance, Mapping):
            raise CardRecordError("provenance_invalid")
        resource_grants = value.get("resource_grants")
        if not isinstance(resource_grants, Mapping):
            raise CardRecordError("resource_grants_invalid")
        operations = value.get("operations")
        if not isinstance(operations, list):
            raise CardRecordError("operations_invalid")
        try:
            resource_operations = (
                project_legacy_operations(resource_grants, operations)
                if schema == CARD_AUTHORITY_SCHEMA_V1
                else normalize_resource_operations(value.get("resource_operations"))
            )
        except ValueError as exc:
            raise CardRecordError("resource_operations_invalid") from exc
        if schema == CARD_AUTHORITY_SCHEMA and not isinstance(
            value.get("resource_operations"), Mapping
        ):
            raise CardRecordError("resource_operations_invalid")
        for name in ("named_services", "account_scope"):
            if not isinstance(value.get(name, {}), Mapping):
                raise CardRecordError(f"{name}_invalid")
        state = clean_text(value.get("state")) or CARD_STATE_ACTIVE
        if state not in (CARD_STATE_ACTIVE, CARD_STATE_REVOKED):
            raise CardRecordError("state_invalid")
        return cls(
            access_id=clean_text(value.get("access_id")),
            client_id=clean_text(value.get("client_id")),
            grantor_subject=clean_text(value.get("grantor_subject")),
            delegate_subject=clean_text(value.get("delegate_subject")),
            source=clean_text(value.get("source")),
            label=clean_text(value.get("label")),
            card_revision=int(value.get("card_revision") or 0),
            catalog_version=clean_text(value.get("catalog_version")),
            state=state,
            operations=(
                tuple(as_string_list(operations))
                if schema == CARD_AUTHORITY_SCHEMA_V1
                else operation_union(resource_operations)
            ),
            resource_grants={
                clean_text(resource): tuple(as_string_list(grants))
                for resource, grants in resource_grants.items()
                if clean_text(resource)
            },
            resource_operations=resource_operations,
            named_service_operations=NamedServiceSelection.from_stored(
                value.get("named_service_operations"),
                present="named_service_operations" in value,
            ),
            named_services=copy.deepcopy(dict(value.get("named_services") or {})),
            account_scope={
                clean_text(provider): {
                    clean_text(account_id): tuple(as_string_list(claims))
                    for account_id, claims in dict(accounts or {}).items()
                    if clean_text(account_id)
                }
                for provider, accounts in dict(value.get("account_scope") or {}).items()
                if clean_text(provider)
            },
            identity_scope=clean_text(value.get("identity_scope")),
            created_at=int(value.get("created_at") or 0),
            expires_at=int(value.get("expires_at") or 0),
            last_issued_at=int(value.get("last_issued_at") or 0),
            last_four=clean_text(value.get("last_four")),
            resource_acceptance=resource_acceptance,
            provenance=copy.deepcopy(dict(provenance)),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": CARD_AUTHORITY_SCHEMA,
            "access_id": self.access_id,
            "client_id": self.client_id,
            "grantor_subject": self.grantor_subject,
            "delegate_subject": self.delegate_subject,
            "source": self.source,
            "label": self.label,
            "card_revision": self.card_revision,
            "catalog_version": self.catalog_version,
            "state": self.state,
            "operations": list(self.operations),
            "resource_grants": {
                resource: list(grants) for resource, grants in self.resource_grants.items()
            },
            "resource_operations": {
                resource: list(operations)
                for resource, operations in self.resource_operations.items()
            },
            "named_services": copy.deepcopy(dict(self.named_services or {})),
            "account_scope": {
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in self.account_scope.items()
            },
            "identity_scope": self.identity_scope,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_issued_at": self.last_issued_at,
            "last_four": self.last_four,
            "resource_acceptance": {
                resource: acceptance.to_dict()
                for resource, acceptance in sorted(self.resource_acceptance.items())
            },
            "provenance": copy.deepcopy(dict(self.provenance or {})),
        }
        stored_selection = self.named_service_operations.to_stored()
        if stored_selection is not None:
            payload["named_service_operations"] = stored_selection
        return payload

    def __post_init__(self) -> None:
        try:
            normalized = normalize_resource_operations(self.resource_operations)
        except ValueError as exc:
            raise CardRecordError("resource_operations_invalid") from exc
        if not normalized and self.operations:
            normalized = project_legacy_operations(
                self.resource_grants, self.operations
            )
        object.__setattr__(self, "resource_operations", normalized)
        object.__setattr__(self, "operations", operation_union(normalized))
        acceptance = {
            clean_text(resource): entry
            for resource, entry in dict(self.resource_acceptance or {}).items()
            if clean_text(resource) and isinstance(entry, ResourceAcceptance)
        }
        object.__setattr__(self, "resource_acceptance", acceptance)
        if not isinstance(self.provenance, Mapping):
            raise CardRecordError("provenance_invalid")

    def content_hash(self) -> str:
        return card_authority_payload_hash(self.to_dict())


@dataclass(frozen=True)
class CardCredentialHandles:
    """Live, bounded credential material kept out of durable card history."""

    access_id: str
    access_token: str = ""
    refresh_token: str = ""
    session_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CardCredentialHandles":
        return cls(
            access_id=clean_text(value.get("access_id")),
            access_token=clean_text(value.get("access_token")),
            refresh_token=clean_text(value.get("refresh_token")),
            session_id=clean_text(value.get("session_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_id": self.access_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "session_id": self.session_id,
        }

    @property
    def empty(self) -> bool:
        return not (self.access_token or self.refresh_token or self.session_id)


def authority_is_usable(authority: "CardAuthority", moment: int) -> bool:
    """Whether this authority may still be served at ``moment``."""
    if authority.state != CARD_STATE_ACTIVE:
        return False
    return authority.expires_at > moment


def card_revision_name(*, card_revision: int, content_hash: str, updated_at: datetime) -> str:
    """Timestamp-first, ordered, verifiable immutable revision object name."""
    return (
        f"card_revision_{utc_stamp(updated_at)}"
        f"_{max(0, int(card_revision)):08d}"
        f"_{content_hash[:_REVISION_HASH_CHARS]}.json"
    )


@dataclass(frozen=True)
class CardCurrentPointer:
    """``current.json``: what is needed to read and trust the latest revision."""

    access_id: str
    card_revision: int
    revision_name: str
    content_hash: str
    updated_at: str
    expires_at: int
    state: str

    @classmethod
    def for_revision(
        cls,
        authority: CardAuthority,
        *,
        revision_name: str,
        content_hash: str,
        updated_at: datetime,
    ) -> "CardCurrentPointer":
        return cls(
            access_id=authority.access_id,
            card_revision=authority.card_revision,
            revision_name=revision_name,
            content_hash=content_hash,
            updated_at=utc_timestamp(updated_at),
            expires_at=authority.expires_at,
            state=authority.state,
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "CardCurrentPointer":
        if not isinstance(value, Mapping):
            raise CardRecordError("pointer_not_object")
        if str(value.get("schema") or "").strip() != CARD_POINTER_SCHEMA:
            raise CardRecordError("pointer_schema_mismatch")
        revision_name = clean_text(value.get("revision_name"))
        if not revision_name:
            raise CardRecordError("pointer_revision_name_missing")
        return cls(
            access_id=clean_text(value.get("access_id")),
            card_revision=int(value.get("card_revision") or 0),
            revision_name=revision_name,
            content_hash=clean_text(value.get("content_hash")).lower(),
            updated_at=clean_text(value.get("updated_at")),
            expires_at=int(value.get("expires_at") or 0),
            state=clean_text(value.get("state")) or CARD_STATE_ACTIVE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CARD_POINTER_SCHEMA,
            "access_id": self.access_id,
            "card_revision": self.card_revision,
            "revision_name": self.revision_name,
            "content_hash": self.content_hash,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "state": self.state,
        }


__all__ = [
    "CARD_AUTHORITY_SCHEMA",
    "CARD_AUTHORITY_SCHEMAS",
    "CARD_AUTHORITY_SCHEMA_V1",
    "CARD_AUTHORITY_SCHEMA_V2",
    "CARD_POINTER_SCHEMA",
    "CARD_STATE_ACTIVE",
    "CARD_STATE_REVOKED",
    "NAMED_SERVICE_OPERATIONS_ALL",
    "SELECTION_ALL",
    "SELECTION_EXACT",
    "SELECTION_NONE",
    "SELECTION_UNKNOWN",
    "CardAuthority",
    "CardCredentialHandles",
    "CardCurrentPointer",
    "CardRecordError",
    "NamedServiceSelection",
    "authority_is_usable",
    "card_authority_payload_hash",
    "card_revision_name",
]

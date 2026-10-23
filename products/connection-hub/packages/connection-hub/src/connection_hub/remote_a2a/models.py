# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Records for user-owned remote A2A connectors.

A connector pins one outside agent's Agent Card: its JSON-RPC endpoint, its
skills and its declared security schemes, under one digest computed by
``app_foundation.a2a``. The pinned card is the connector's descriptor; a card
whose digest moved is pending until the owner accepts it again, exactly as a
remote MCP connector's descriptor drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app_foundation.a2a import AgentSkill, PinnedAgentCard

from connection_hub.remote_mcp.models import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    CONNECTOR_ACTIVE,
    CONNECTOR_DELETED,
    CONNECTOR_DISABLED,
    DESCRIPTOR_ACCEPTED,
    DESCRIPTOR_DRIFTED,
    validated_auth_header,
)

CONNECTOR_SCHEMA = "connection_hub.remote_a2a.connector.v1"
POINTER_SCHEMA = "connection_hub.remote_a2a.current.v1"
TASK_SCHEMA = "connection_hub.remote_a2a.task.v1"
CONTEXT_SCHEMA = "connection_hub.remote_a2a.context.v1"
RESOURCE_PREFIX = "urn:connection-hub:remote-a2a:"
EXTERNAL_A2A_GRANT = "external_a2a:use"
A2A_AUTH_MODES = frozenset({AUTH_NONE, AUTH_BEARER, AUTH_HEADER})

SEND_OPERATION_PREFIX = "send_"
GET_TASK_OPERATION = "get_task"
CANCEL_TASK_OPERATION = "cancel_task"
REPLY_TASK_OPERATION = "reply_task"
TASK_OPERATIONS = (GET_TASK_OPERATION, CANCEL_TASK_OPERATION, REPLY_TASK_OPERATION)

_CONNECTOR_ID_PATTERN = re.compile(r"^a2a_[a-z0-9]{24}$")


class RemoteA2ARecordError(ValueError):
    """A connector record or pinned card is not usable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validated_connector_id(value: Any) -> str:
    connector_id = _clean(value).lower()
    if not _CONNECTOR_ID_PATTERN.fullmatch(connector_id):
        raise RemoteA2ARecordError("connector_id_invalid")
    return connector_id


def connector_resource(connector_id: str) -> str:
    return f"{RESOURCE_PREFIX}{validated_connector_id(connector_id)}"


def connector_id_from_resource(resource: Any) -> str:
    value = _clean(resource)
    if not value.startswith(RESOURCE_PREFIX):
        return ""
    try:
        return validated_connector_id(value[len(RESOURCE_PREFIX) :])
    except RemoteA2ARecordError:
        return ""


def send_operation(skill_id: str) -> str:
    return f"{SEND_OPERATION_PREFIX}{skill_id}"


def card_to_mapping(card: PinnedAgentCard) -> dict[str, Any]:
    return {
        "card_url": card.card_url,
        **card.pinned_fields(),
        "dropped_skills": int(card.dropped_skills),
        "digest": card.digest,
    }


def card_from_mapping(value: Any) -> PinnedAgentCard:
    if not isinstance(value, Mapping):
        raise RemoteA2ARecordError("card_not_object")
    try:
        skills = tuple(
            AgentSkill(
                skill_id=_clean(item["id"]),
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                tags=tuple(str(tag) for tag in item.get("tags") or ()),
            )
            for item in value.get("skills") or ()
        )
        card = PinnedAgentCard(
            card_url=_clean(value.get("card_url")),
            name=str(value.get("name") or ""),
            description=str(value.get("description") or ""),
            version=str(value.get("version") or ""),
            jsonrpc_url=_clean(value.get("jsonrpc_url")),
            protocol_version=_clean(value.get("protocol_version")),
            skills=skills,
            security_schemes=tuple(
                (str(item[0]), str(item[1])) for item in value.get("security_schemes") or ()
            ),
            dropped_skills=int(value.get("dropped_skills") or 0),
            digest=_clean(value.get("digest")).lower(),
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise RemoteA2ARecordError("card_invalid") from exc
    from app_foundation.a2a import card_digest

    if card_digest(card.pinned_fields()) != card.digest:
        raise RemoteA2ARecordError("card_digest_mismatch")
    return card


def operation_digest(card_digest_value: str, operation: str) -> str:
    """Every operation answers to the whole pinned card: a card change is drift."""

    return hashlib.sha256(f"{card_digest_value}\n{operation}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RemoteA2AConnector:
    connector_id: str
    owner_subject: str
    label: str
    card_url: str
    resource: str
    revision: int
    state: str
    credential_mode: str = AUTH_NONE
    credential_header: str = ""
    credential_ref: str = ""
    card: PinnedAgentCard | None = None
    descriptor_revision: int = 1
    descriptor_state: str = DESCRIPTOR_ACCEPTED
    pending_card: PinnedAgentCard | None = None
    created_at: int = 0
    updated_at: int = 0
    last_checked_at: int = 0
    last_error: str = ""

    @property
    def card_digest(self) -> str:
        return self.card.digest if self.card is not None else ""

    def operations(self) -> tuple[str, ...]:
        if self.card is None:
            return ()
        return tuple(send_operation(skill.skill_id) for skill in self.card.skills) + TASK_OPERATIONS

    def operation_digests(self) -> dict[str, str]:
        return {operation: operation_digest(self.card_digest, operation) for operation in self.operations()}

    def verify(self) -> None:
        validated_connector_id(self.connector_id)
        if self.resource != connector_resource(self.connector_id):
            raise RemoteA2ARecordError("connector_resource_mismatch")
        if not self.owner_subject:
            raise RemoteA2ARecordError("owner_subject_missing")
        if not self.label or len(self.label) > 160:
            raise RemoteA2ARecordError("connector_label_invalid")
        if self.state not in {CONNECTOR_ACTIVE, CONNECTOR_DISABLED, CONNECTOR_DELETED}:
            raise RemoteA2ARecordError("connector_state_invalid")
        if self.credential_mode not in A2A_AUTH_MODES:
            raise RemoteA2ARecordError("credential_mode_invalid")
        if self.credential_mode == AUTH_HEADER:
            validated_auth_header(self.credential_header)
        if (self.credential_mode == AUTH_NONE) != (not self.credential_ref):
            raise RemoteA2ARecordError("credential_ref_mismatch")
        if self.descriptor_state not in {DESCRIPTOR_ACCEPTED, DESCRIPTOR_DRIFTED}:
            raise RemoteA2ARecordError("descriptor_state_invalid")
        if self.state != CONNECTOR_DELETED and self.card is None:
            raise RemoteA2ARecordError("connector_card_missing")
        if (self.descriptor_state == DESCRIPTOR_DRIFTED) != (self.pending_card is not None):
            raise RemoteA2ARecordError("pending_card_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONNECTOR_SCHEMA,
            "connector_id": self.connector_id,
            "owner_subject": self.owner_subject,
            "label": self.label,
            "card_url": self.card_url,
            "resource": self.resource,
            "revision": int(self.revision),
            "state": self.state,
            "credential_mode": self.credential_mode,
            "credential_header": self.credential_header,
            "credential_ref": self.credential_ref,
            "card": card_to_mapping(self.card) if self.card is not None else None,
            "descriptor_revision": int(self.descriptor_revision),
            "descriptor_state": self.descriptor_state,
            "pending_card": card_to_mapping(self.pending_card) if self.pending_card is not None else None,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "last_checked_at": int(self.last_checked_at),
            "last_error": self.last_error,
        }

    def content_hash(self) -> str:
        return _canonical_hash(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteA2AConnector":
        if not isinstance(value, Mapping):
            raise RemoteA2ARecordError("connector_not_object")
        if _clean(value.get("schema")) != CONNECTOR_SCHEMA:
            raise RemoteA2ARecordError("schema_mismatch")
        try:
            connector = cls(
                connector_id=validated_connector_id(value.get("connector_id")),
                owner_subject=_clean(value.get("owner_subject")),
                label=_clean(value.get("label")),
                card_url=_clean(value.get("card_url")),
                resource=_clean(value.get("resource")),
                revision=int(value.get("revision") or 0),
                state=_clean(value.get("state")),
                credential_mode=_clean(value.get("credential_mode")).lower() or AUTH_NONE,
                credential_header=_clean(value.get("credential_header")),
                credential_ref=_clean(value.get("credential_ref")),
                card=card_from_mapping(value["card"]) if value.get("card") is not None else None,
                descriptor_revision=int(value.get("descriptor_revision") or 0),
                descriptor_state=_clean(value.get("descriptor_state")) or DESCRIPTOR_ACCEPTED,
                pending_card=(
                    card_from_mapping(value["pending_card"])
                    if value.get("pending_card") is not None
                    else None
                ),
                created_at=int(value.get("created_at") or 0),
                updated_at=int(value.get("updated_at") or 0),
                last_checked_at=int(value.get("last_checked_at") or 0),
                last_error=_clean(value.get("last_error")),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, RemoteA2ARecordError):
                raise
            raise RemoteA2ARecordError("connector_invalid") from exc
        connector.verify()
        return connector

    def to_public_dict(self) -> dict[str, Any]:
        card = self.card
        pending = self.pending_card
        return {
            "connector_id": self.connector_id,
            "label": self.label,
            "card_url": self.card_url,
            "resource": self.resource,
            "revision": int(self.revision),
            "state": self.state,
            "credential_mode": self.credential_mode,
            "credential_header": self.credential_header,
            "agent": (
                {
                    "name": card.name,
                    "description": card.description,
                    "version": card.version,
                    "endpoint": card.jsonrpc_url,
                    "protocol_version": card.protocol_version,
                    "skills": [
                        {"id": skill.skill_id, "name": skill.name, "description": skill.description}
                        for skill in card.skills
                    ],
                    "dropped_skills": card.dropped_skills,
                    "security_schemes": [list(item) for item in card.security_schemes],
                }
                if card is not None
                else None
            ),
            "descriptor_revision": int(self.descriptor_revision),
            "descriptor_state": self.descriptor_state,
            "pending_agent": (
                {
                    "endpoint": pending.jsonrpc_url,
                    "version": pending.version,
                    "skills": [skill.skill_id for skill in pending.skills],
                }
                if pending is not None
                else None
            ),
            "operations": list(self.operations()),
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "last_checked_at": int(self.last_checked_at),
        }


@dataclass(frozen=True)
class RemoteA2ACurrentPointer:
    connector_id: str
    revision: int
    revision_name: str
    content_hash: str
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POINTER_SCHEMA,
            "connector_id": self.connector_id,
            "revision": int(self.revision),
            "revision_name": self.revision_name,
            "content_hash": self.content_hash,
            "updated_at": int(self.updated_at),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteA2ACurrentPointer":
        if not isinstance(value, Mapping) or _clean(value.get("schema")) != POINTER_SCHEMA:
            raise RemoteA2ARecordError("pointer_invalid")
        return cls(
            connector_id=validated_connector_id(value.get("connector_id")),
            revision=int(value.get("revision") or 0),
            revision_name=_clean(value.get("revision_name")),
            content_hash=_clean(value.get("content_hash")).lower(),
            updated_at=int(value.get("updated_at") or 0),
        )


@dataclass(frozen=True)
class RemoteA2ATaskRecord:
    """Which Card created one remote task. Only that Card may read or act on it."""

    connector_id: str
    task_id: str
    access_id: str
    context_id: str = ""
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_SCHEMA,
            "connector_id": self.connector_id,
            "task_id": self.task_id,
            "access_id": self.access_id,
            "context_id": self.context_id,
            "created_at": int(self.created_at),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteA2ATaskRecord":
        if not isinstance(value, Mapping) or _clean(value.get("schema")) != TASK_SCHEMA:
            raise RemoteA2ARecordError("task_record_invalid")
        record = cls(
            connector_id=validated_connector_id(value.get("connector_id")),
            task_id=_clean(value.get("task_id")),
            access_id=_clean(value.get("access_id")),
            context_id=_clean(value.get("context_id")),
            created_at=int(value.get("created_at") or 0),
        )
        if not record.task_id or not record.access_id:
            raise RemoteA2ARecordError("task_record_invalid")
        return record


@dataclass(frozen=True)
class RemoteA2AContextRecord:
    """Which Card first used one remote context. Only that Card may continue it."""

    connector_id: str
    context_id: str
    access_id: str
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_SCHEMA,
            "connector_id": self.connector_id,
            "context_id": self.context_id,
            "access_id": self.access_id,
            "created_at": int(self.created_at),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteA2AContextRecord":
        if not isinstance(value, Mapping) or _clean(value.get("schema")) != CONTEXT_SCHEMA:
            raise RemoteA2ARecordError("context_record_invalid")
        record = cls(
            connector_id=validated_connector_id(value.get("connector_id")),
            context_id=_clean(value.get("context_id")),
            access_id=_clean(value.get("access_id")),
            created_at=int(value.get("created_at") or 0),
        )
        if not record.context_id or not record.access_id:
            raise RemoteA2ARecordError("context_record_invalid")
        return record


__all__ = [
    "A2A_AUTH_MODES",
    "RemoteA2AContextRecord",
    "CANCEL_TASK_OPERATION",
    "CONNECTOR_SCHEMA",
    "EXTERNAL_A2A_GRANT",
    "GET_TASK_OPERATION",
    "REPLY_TASK_OPERATION",
    "RESOURCE_PREFIX",
    "RemoteA2AConnector",
    "RemoteA2ACurrentPointer",
    "RemoteA2ARecordError",
    "RemoteA2ATaskRecord",
    "SEND_OPERATION_PREFIX",
    "TASK_OPERATIONS",
    "card_from_mapping",
    "card_to_mapping",
    "connector_id_from_resource",
    "connector_resource",
    "operation_digest",
    "send_operation",
    "validated_connector_id",
]

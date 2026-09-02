# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable, non-secret records for user-owned remote MCP connectors."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

CONNECTOR_SCHEMA = "connection_hub.remote_mcp_connector.v1"
CONNECTOR_POINTER_SCHEMA = "connection_hub.remote_mcp_connector_current.v1"

CONNECTOR_ACTIVE = "active"
CONNECTOR_DISABLED = "disabled"
CONNECTOR_DELETED = "deleted"

DESCRIPTOR_ACCEPTED = "accepted"
DESCRIPTOR_DRIFTED = "drifted"

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_HEADER = "header"

RESOURCE_PREFIX = "urn:connection-hub:remote-mcp:"

_CONNECTOR_ID_PATTERN = re.compile(r"^mcp_[a-z0-9]{24}$")
_HEADER_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_TOOL_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_BLOCKED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "cookie",
        "forwarded",
        "host",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)


class RemoteMCPRecordError(ValueError):
    """A connector record or discovered descriptor is not usable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any, *, reason: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RemoteMCPRecordError(reason)
    return copy.deepcopy(dict(value))


def _canonical_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RemoteMCPRecordError("descriptor_not_json") from exc
    return hashlib.sha256(payload).hexdigest()


def validated_connector_id(value: Any) -> str:
    connector_id = _clean(value).lower()
    if not _CONNECTOR_ID_PATTERN.fullmatch(connector_id):
        raise RemoteMCPRecordError("connector_id_invalid")
    return connector_id


def connector_resource(connector_id: str) -> str:
    return f"{RESOURCE_PREFIX}{validated_connector_id(connector_id)}"


def connector_id_from_resource(resource: Any) -> str:
    value = _clean(resource)
    if not value.startswith(RESOURCE_PREFIX):
        return ""
    try:
        return validated_connector_id(value[len(RESOURCE_PREFIX) :])
    except RemoteMCPRecordError:
        return ""


def validated_auth_header(value: Any) -> str:
    header = _clean(value)
    if not _HEADER_PATTERN.fullmatch(header):
        raise RemoteMCPRecordError("credential_header_invalid")
    if header.lower() in _BLOCKED_HEADERS:
        raise RemoteMCPRecordError("credential_header_forbidden")
    return header


def proxy_tool_name(connector_id: str, tool_name: str) -> str:
    connector = validated_connector_id(connector_id)
    original = _clean(tool_name)
    if not original:
        raise RemoteMCPRecordError("tool_name_missing")
    slug = _TOOL_SLUG_PATTERN.sub("_", original).strip("._-") or "tool"
    slug = slug[:48]
    suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    return f"{connector}__{slug}_{suffix}"


@dataclass(frozen=True)
class RemoteMCPCredential:
    """An in-memory upstream credential. Its value is never serialized."""

    mode: str = AUTH_NONE
    value: str = field(default="", repr=False)
    header: str = ""

    def request_headers(self) -> dict[str, str]:
        mode = _clean(self.mode).lower() or AUTH_NONE
        if mode == AUTH_NONE:
            return {}
        if not self.value:
            raise RemoteMCPRecordError("credential_value_missing")
        if mode == AUTH_BEARER:
            return {"Authorization": f"Bearer {self.value}"}
        if mode == AUTH_HEADER:
            return {validated_auth_header(self.header): self.value}
        raise RemoteMCPRecordError("credential_mode_invalid")


@dataclass(frozen=True)
class RemoteMCPTool:
    name: str
    proxy_name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    descriptor_digest: str = ""

    @classmethod
    def build(
        cls,
        *,
        connector_id: str,
        name: Any,
        description: Any = "",
        input_schema: Any = None,
        output_schema: Any = None,
    ) -> "RemoteMCPTool":
        tool_name = _clean(name)
        if not tool_name or len(tool_name) > 256:
            raise RemoteMCPRecordError("tool_name_invalid")
        inputs = _mapping(input_schema, reason="tool_input_schema_invalid")
        outputs = (
            None
            if output_schema is None
            else _mapping(output_schema, reason="tool_output_schema_invalid")
        )
        descriptor = {
            "name": tool_name,
            "description": _clean(description),
            "input_schema": inputs,
            "output_schema": outputs,
        }
        return cls(
            name=tool_name,
            proxy_name=proxy_tool_name(connector_id, tool_name),
            description=descriptor["description"],
            input_schema=inputs,
            output_schema=outputs,
            descriptor_digest=_canonical_hash(descriptor),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteMCPTool":
        if not isinstance(value, Mapping):
            raise RemoteMCPRecordError("tool_not_object")
        tool = cls(
            name=_clean(value.get("name")),
            proxy_name=_clean(value.get("proxy_name")),
            description=_clean(value.get("description")),
            input_schema=_mapping(
                value.get("input_schema"), reason="tool_input_schema_invalid"
            ),
            output_schema=(
                None
                if value.get("output_schema") is None
                else _mapping(
                    value.get("output_schema"), reason="tool_output_schema_invalid"
                )
            ),
            descriptor_digest=_clean(value.get("descriptor_digest")).lower(),
        )
        if not tool.name or not tool.proxy_name or len(tool.descriptor_digest) != 64:
            raise RemoteMCPRecordError("tool_invalid")
        return tool

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "proxy_name": self.proxy_name,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
            "descriptor_digest": self.descriptor_digest,
        }
        if self.output_schema is not None:
            payload["output_schema"] = copy.deepcopy(dict(self.output_schema))
        return payload


@dataclass(frozen=True)
class RemoteMCPDiscovery:
    tools: tuple[RemoteMCPTool, ...]
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""
    descriptor_digest: str = ""

    @classmethod
    def build(
        cls,
        *,
        connector_id: str,
        tools: Iterable[Any],
        server_name: Any = "",
        server_version: Any = "",
        protocol_version: Any = "",
    ) -> "RemoteMCPDiscovery":
        normalized: list[RemoteMCPTool] = []
        seen: set[str] = set()
        for raw in tools or ():
            if isinstance(raw, RemoteMCPTool):
                tool = RemoteMCPTool.build(
                    connector_id=connector_id,
                    name=raw.name,
                    description=raw.description,
                    input_schema=raw.input_schema,
                    output_schema=raw.output_schema,
                )
            elif isinstance(raw, Mapping):
                tool = RemoteMCPTool.build(
                    connector_id=connector_id,
                    name=raw.get("name"),
                    description=raw.get("description"),
                    input_schema=raw.get("input_schema") or raw.get("inputSchema"),
                    output_schema=raw.get("output_schema") or raw.get("outputSchema"),
                )
            else:
                raise RemoteMCPRecordError("discovered_tool_invalid")
            if tool.name in seen:
                raise RemoteMCPRecordError("duplicate_tool_name")
            seen.add(tool.name)
            normalized.append(tool)
        normalized.sort(key=lambda item: item.name)
        identity = {
            "server_name": _clean(server_name),
            "server_version": _clean(server_version),
            "protocol_version": _clean(protocol_version),
            "tools": [tool.to_dict() for tool in normalized],
        }
        return cls(
            tools=tuple(normalized),
            server_name=identity["server_name"],
            server_version=identity["server_version"],
            protocol_version=identity["protocol_version"],
            descriptor_digest=_canonical_hash(identity),
        )

    def tool_map(self) -> dict[str, RemoteMCPTool]:
        return {tool.name: tool for tool in self.tools}


def descriptor_drift(
    accepted: Iterable[RemoteMCPTool], observed: Iterable[RemoteMCPTool]
) -> dict[str, list[str]]:
    known = {tool.name: tool for tool in accepted}
    current = {tool.name: tool for tool in observed}
    return {
        "added": sorted(set(current) - set(known)),
        "changed": sorted(
            name
            for name in set(known) & set(current)
            if known[name].descriptor_digest != current[name].descriptor_digest
        ),
        "removed": sorted(set(known) - set(current)),
    }


@dataclass(frozen=True)
class RemoteMCPConnector:
    connector_id: str
    owner_subject: str
    label: str
    endpoint: str
    transport: str
    resource: str
    revision: int
    state: str
    credential_mode: str
    credential_header: str = ""
    credential_ref: str = ""
    tools: tuple[RemoteMCPTool, ...] = ()
    descriptor_digest: str = ""
    descriptor_revision: int = 1
    descriptor_state: str = DESCRIPTOR_ACCEPTED
    pending_tools: tuple[RemoteMCPTool, ...] = ()
    pending_descriptor_digest: str = ""
    drift: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""
    created_at: int = 0
    updated_at: int = 0
    last_checked_at: int = 0
    last_error: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteMCPConnector":
        if not isinstance(value, Mapping):
            raise RemoteMCPRecordError("connector_not_object")
        if _clean(value.get("schema")) != CONNECTOR_SCHEMA:
            raise RemoteMCPRecordError("schema_mismatch")
        connector_id = validated_connector_id(value.get("connector_id"))
        state = _clean(value.get("state"))
        if state not in {CONNECTOR_ACTIVE, CONNECTOR_DISABLED, CONNECTOR_DELETED}:
            raise RemoteMCPRecordError("connector_state_invalid")
        auth_mode = _clean(value.get("credential_mode")).lower() or AUTH_NONE
        if auth_mode not in {AUTH_NONE, AUTH_BEARER, AUTH_HEADER}:
            raise RemoteMCPRecordError("credential_mode_invalid")
        auth_header = _clean(value.get("credential_header"))
        if auth_mode == AUTH_HEADER:
            auth_header = validated_auth_header(auth_header)
        descriptor_state = _clean(value.get("descriptor_state")) or DESCRIPTOR_ACCEPTED
        if descriptor_state not in {DESCRIPTOR_ACCEPTED, DESCRIPTOR_DRIFTED}:
            raise RemoteMCPRecordError("descriptor_state_invalid")
        tools_raw = value.get("tools")
        pending_raw = value.get("pending_tools", [])
        if not isinstance(tools_raw, list) or not isinstance(pending_raw, list):
            raise RemoteMCPRecordError("connector_tools_invalid")
        drift_raw = value.get("drift") or {}
        if not isinstance(drift_raw, Mapping):
            raise RemoteMCPRecordError("connector_drift_invalid")
        connector = cls(
            connector_id=connector_id,
            owner_subject=_clean(value.get("owner_subject")),
            label=_clean(value.get("label")),
            endpoint=_clean(value.get("endpoint")),
            transport=_clean(value.get("transport")) or "streamable-http",
            resource=_clean(value.get("resource")),
            revision=int(value.get("revision") or 0),
            state=state,
            credential_mode=auth_mode,
            credential_header=auth_header,
            credential_ref=_clean(value.get("credential_ref")),
            tools=tuple(RemoteMCPTool.from_mapping(item) for item in tools_raw),
            descriptor_digest=_clean(value.get("descriptor_digest")).lower(),
            descriptor_revision=int(value.get("descriptor_revision") or 0),
            descriptor_state=descriptor_state,
            pending_tools=tuple(
                RemoteMCPTool.from_mapping(item) for item in pending_raw
            ),
            pending_descriptor_digest=_clean(
                value.get("pending_descriptor_digest")
            ).lower(),
            drift={
                str(key): tuple(_clean(item) for item in raw if _clean(item))
                for key, raw in drift_raw.items()
                if isinstance(raw, (list, tuple))
            },
            server_name=_clean(value.get("server_name")),
            server_version=_clean(value.get("server_version")),
            protocol_version=_clean(value.get("protocol_version")),
            created_at=int(value.get("created_at") or 0),
            updated_at=int(value.get("updated_at") or 0),
            last_checked_at=int(value.get("last_checked_at") or 0),
            last_error=_clean(value.get("last_error")),
        )
        connector.verify()
        return connector

    def verify(self) -> None:
        if not self.owner_subject:
            raise RemoteMCPRecordError("owner_subject_missing")
        if not self.label or len(self.label) > 160:
            raise RemoteMCPRecordError("connector_label_invalid")
        if not self.endpoint:
            raise RemoteMCPRecordError("connector_endpoint_missing")
        if self.transport != "streamable-http":
            raise RemoteMCPRecordError("connector_transport_invalid")
        if self.resource != connector_resource(self.connector_id):
            raise RemoteMCPRecordError("connector_resource_invalid")
        if self.revision < 1 or self.descriptor_revision < 1:
            raise RemoteMCPRecordError("connector_revision_invalid")
        if len(self.descriptor_digest) != 64:
            raise RemoteMCPRecordError("descriptor_digest_invalid")
        if self.credential_mode == AUTH_NONE and self.credential_ref:
            raise RemoteMCPRecordError("credential_ref_unexpected")
        if self.credential_mode != AUTH_NONE and not self.credential_ref:
            raise RemoteMCPRecordError("credential_ref_missing")
        if self.descriptor_state == DESCRIPTOR_DRIFTED:
            if len(self.pending_descriptor_digest) != 64:
                raise RemoteMCPRecordError("pending_descriptor_digest_invalid")
        elif self.pending_tools or self.pending_descriptor_digest or self.drift:
            raise RemoteMCPRecordError("pending_descriptor_unexpected")
        names = [tool.name for tool in self.tools]
        aliases = [tool.proxy_name for tool in self.tools]
        if len(names) != len(set(names)) or len(aliases) != len(set(aliases)):
            raise RemoteMCPRecordError("connector_tools_duplicate")

    @property
    def credential_present(self) -> bool:
        return self.credential_mode != AUTH_NONE and bool(self.credential_ref)

    def tool_map(self) -> dict[str, RemoteMCPTool]:
        return {tool.name: tool for tool in self.tools}

    def proxy_tool_map(self) -> dict[str, RemoteMCPTool]:
        return {tool.proxy_name: tool for tool in self.tools}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONNECTOR_SCHEMA,
            "connector_id": self.connector_id,
            "owner_subject": self.owner_subject,
            "label": self.label,
            "endpoint": self.endpoint,
            "transport": self.transport,
            "resource": self.resource,
            "revision": self.revision,
            "state": self.state,
            "credential_mode": self.credential_mode,
            "credential_header": self.credential_header,
            "credential_ref": self.credential_ref,
            "tools": [tool.to_dict() for tool in self.tools],
            "descriptor_digest": self.descriptor_digest,
            "descriptor_revision": self.descriptor_revision,
            "descriptor_state": self.descriptor_state,
            "pending_tools": [tool.to_dict() for tool in self.pending_tools],
            "pending_descriptor_digest": self.pending_descriptor_digest,
            "drift": {key: list(values) for key, values in self.drift.items()},
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "label": self.label,
            "endpoint": self.endpoint,
            "transport": self.transport,
            "resource": self.resource,
            "revision": self.revision,
            "state": self.state,
            "credential_mode": self.credential_mode,
            "credential_header": self.credential_header,
            "credential_present": self.credential_present,
            "tools": [tool.to_dict() for tool in self.tools],
            "descriptor_digest": self.descriptor_digest,
            "descriptor_revision": self.descriptor_revision,
            "descriptor_state": self.descriptor_state,
            "pending_tools": [tool.to_dict() for tool in self.pending_tools],
            "pending_descriptor_digest": self.pending_descriptor_digest,
            "drift": {key: list(values) for key, values in self.drift.items()},
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
        }

    def content_hash(self) -> str:
        return _canonical_hash(self.to_dict())


@dataclass(frozen=True)
class RemoteMCPCurrentPointer:
    connector_id: str
    revision: int
    revision_name: str
    content_hash: str
    updated_at: int

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteMCPCurrentPointer":
        if not isinstance(value, Mapping):
            raise RemoteMCPRecordError("pointer_not_object")
        if _clean(value.get("schema")) != CONNECTOR_POINTER_SCHEMA:
            raise RemoteMCPRecordError("pointer_schema_mismatch")
        pointer = cls(
            connector_id=validated_connector_id(value.get("connector_id")),
            revision=int(value.get("revision") or 0),
            revision_name=_clean(value.get("revision_name")),
            content_hash=_clean(value.get("content_hash")).lower(),
            updated_at=int(value.get("updated_at") or 0),
        )
        if pointer.revision < 1 or not pointer.revision_name:
            raise RemoteMCPRecordError("pointer_invalid")
        if len(pointer.content_hash) != 64:
            raise RemoteMCPRecordError("pointer_hash_invalid")
        return pointer

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONNECTOR_POINTER_SCHEMA,
            "connector_id": self.connector_id,
            "revision": self.revision,
            "revision_name": self.revision_name,
            "content_hash": self.content_hash,
            "updated_at": self.updated_at,
        }


__all__ = [
    "AUTH_BEARER",
    "AUTH_HEADER",
    "AUTH_NONE",
    "CONNECTOR_ACTIVE",
    "CONNECTOR_DELETED",
    "CONNECTOR_DISABLED",
    "CONNECTOR_POINTER_SCHEMA",
    "CONNECTOR_SCHEMA",
    "DESCRIPTOR_ACCEPTED",
    "DESCRIPTOR_DRIFTED",
    "RESOURCE_PREFIX",
    "RemoteMCPConnector",
    "RemoteMCPCredential",
    "RemoteMCPCurrentPointer",
    "RemoteMCPDiscovery",
    "RemoteMCPRecordError",
    "RemoteMCPTool",
    "connector_id_from_resource",
    "connector_resource",
    "descriptor_drift",
    "proxy_tool_name",
    "validated_auth_header",
    "validated_connector_id",
]

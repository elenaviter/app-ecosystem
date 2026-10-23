# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Immutable A2A connector revisions and task ownership, in bundle storage.

The layout mirrors the remote MCP connector store under its own root:
``remote-a2a-connectors/v1/owners/<sha256 owner>/connectors/<id>/`` holds the
revisions and ``current.json``; ``tasks/<sha256 task id>.json`` records which
Card created each remote task.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
from typing import Protocol

from connection_hub.delegated_credentials.durable_io import (
    DurableStorageError,
    list_child_names,
    read_json_or_none,
    write_json_atomic,
)
from connection_hub.remote_a2a.models import (
    RemoteA2AConnector,
    RemoteA2AContextRecord,
    RemoteA2ACurrentPointer,
    RemoteA2ARecordError,
    RemoteA2ATaskRecord,
    validated_connector_id,
)
from connection_hub.remote_mcp.store import owner_hash_for, validated_owner_hash

CONNECTORS_DIRNAME = "remote-a2a-connectors"
CONNECTORS_LAYOUT_VERSION = "v1"
OWNERS_DIRNAME = "owners"
CONNECTORS_SUBDIRNAME = "connectors"
REVISIONS_DIRNAME = "revisions"
TASKS_DIRNAME = "tasks"
CONTEXTS_DIRNAME = "contexts"
CURRENT_FILENAME = "current.json"

_REVISION_NAME_PATTERN = re.compile(r"^connector_revision_[0-9]{10}_[0-9]{8}_[0-9a-f]{12}\.json$")


class RemoteA2AStorageError(DurableStorageError):
    """Durable A2A connector storage could not be read or written."""


def validated_revision_name(value: str) -> str:
    name = str(value or "").strip()
    if not _REVISION_NAME_PATTERN.fullmatch(name):
        raise RemoteA2AStorageError("revision_name_invalid")
    return name


def task_key(task_id: str) -> str:
    return hashlib.sha256(str(task_id or "").encode("utf-8")).hexdigest()


class RemoteA2AConnectorStore(Protocol):
    def connector_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path: ...

    async def read_current_connector(
        self, *, owner_hash: str, connector_id: str
    ) -> tuple[RemoteA2ACurrentPointer, RemoteA2AConnector] | None: ...

    async def write_revision(self, *, owner_hash: str, connector: RemoteA2AConnector) -> RemoteA2ACurrentPointer: ...

    async def advance_current(self, *, owner_hash: str, pointer: RemoteA2ACurrentPointer) -> None: ...

    async def list_connector_ids(self, *, owner_hash: str) -> list[str]: ...

    async def read_task(self, *, owner_hash: str, connector_id: str, task_id: str) -> RemoteA2ATaskRecord | None: ...

    async def write_task(self, *, owner_hash: str, record: RemoteA2ATaskRecord) -> None: ...

    async def read_context(self, *, owner_hash: str, connector_id: str, context_id: str) -> RemoteA2AContextRecord | None: ...

    async def write_context(self, *, owner_hash: str, record: RemoteA2AContextRecord) -> None: ...


class BundleStorageRemoteA2AConnectorStore:
    def __init__(self, storage_root: str | os.PathLike[str]) -> None:
        self._root = pathlib.Path(storage_root) / CONNECTORS_DIRNAME / CONNECTORS_LAYOUT_VERSION

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def owner_path(self, owner_hash: str) -> pathlib.Path:
        return self._root / OWNERS_DIRNAME / validated_owner_hash(owner_hash) / CONNECTORS_SUBDIRNAME

    def connector_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path:
        return self.owner_path(owner_hash) / validated_connector_id(connector_id)

    def _current_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path:
        return self.connector_path(owner_hash=owner_hash, connector_id=connector_id) / CURRENT_FILENAME

    def _revision_path(self, *, owner_hash: str, connector_id: str, revision_name: str) -> pathlib.Path:
        return (
            self.connector_path(owner_hash=owner_hash, connector_id=connector_id)
            / REVISIONS_DIRNAME
            / validated_revision_name(revision_name)
        )

    def _task_path(self, *, owner_hash: str, connector_id: str, task_id: str) -> pathlib.Path:
        return (
            self.connector_path(owner_hash=owner_hash, connector_id=connector_id)
            / TASKS_DIRNAME
            / f"{task_key(task_id)}.json"
        )

    def _context_path(self, *, owner_hash: str, connector_id: str, context_id: str) -> pathlib.Path:
        return (
            self.connector_path(owner_hash=owner_hash, connector_id=connector_id)
            / CONTEXTS_DIRNAME
            / f"{task_key(context_id)}.json"
        )

    async def read_current_connector(
        self, *, owner_hash: str, connector_id: str
    ) -> tuple[RemoteA2ACurrentPointer, RemoteA2AConnector] | None:
        pointer_payload = await read_json_or_none(
            self._current_path(owner_hash=owner_hash, connector_id=connector_id)
        )
        if pointer_payload is None:
            return None
        pointer = RemoteA2ACurrentPointer.from_mapping(pointer_payload)
        payload = await read_json_or_none(
            self._revision_path(
                owner_hash=owner_hash, connector_id=connector_id, revision_name=pointer.revision_name
            )
        )
        if payload is None:
            raise RemoteA2AStorageError("current_revision_missing")
        connector = RemoteA2AConnector.from_mapping(payload)
        if connector.content_hash() != pointer.content_hash:
            raise RemoteA2ARecordError("revision_content_hash_mismatch")
        if connector.revision != pointer.revision:
            raise RemoteA2ARecordError("revision_number_mismatch")
        return pointer, connector

    async def write_revision(self, *, owner_hash: str, connector: RemoteA2AConnector) -> RemoteA2ACurrentPointer:
        content_hash = connector.content_hash()
        revision_name = (
            f"connector_revision_{int(connector.updated_at):010d}_"
            f"{int(connector.revision):08d}_{content_hash[:12]}.json"
        )
        pointer = RemoteA2ACurrentPointer(
            connector_id=connector.connector_id,
            revision=connector.revision,
            revision_name=revision_name,
            content_hash=content_hash,
            updated_at=connector.updated_at,
        )
        await write_json_atomic(
            self._revision_path(
                owner_hash=owner_hash, connector_id=connector.connector_id, revision_name=revision_name
            ),
            connector.to_dict(),
        )
        return pointer

    async def advance_current(self, *, owner_hash: str, pointer: RemoteA2ACurrentPointer) -> None:
        await write_json_atomic(
            self._current_path(owner_hash=owner_hash, connector_id=pointer.connector_id),
            pointer.to_dict(),
        )

    async def list_connector_ids(self, *, owner_hash: str) -> list[str]:
        out: list[str] = []
        for name in await list_child_names(self.owner_path(owner_hash)):
            try:
                out.append(validated_connector_id(name))
            except RemoteA2ARecordError:
                continue
        return out

    async def read_task(self, *, owner_hash: str, connector_id: str, task_id: str) -> RemoteA2ATaskRecord | None:
        payload = await read_json_or_none(
            self._task_path(owner_hash=owner_hash, connector_id=connector_id, task_id=task_id)
        )
        if payload is None:
            return None
        record = RemoteA2ATaskRecord.from_mapping(payload)
        if record.connector_id != validated_connector_id(connector_id) or record.task_id != task_id:
            raise RemoteA2ARecordError("task_record_mismatch")
        return record

    async def write_task(self, *, owner_hash: str, record: RemoteA2ATaskRecord) -> None:
        await write_json_atomic(
            self._task_path(owner_hash=owner_hash, connector_id=record.connector_id, task_id=record.task_id),
            record.to_dict(),
        )

    async def read_context(self, *, owner_hash: str, connector_id: str, context_id: str) -> RemoteA2AContextRecord | None:
        payload = await read_json_or_none(
            self._context_path(owner_hash=owner_hash, connector_id=connector_id, context_id=context_id)
        )
        if payload is None:
            return None
        record = RemoteA2AContextRecord.from_mapping(payload)
        if record.connector_id != validated_connector_id(connector_id) or record.context_id != context_id:
            raise RemoteA2ARecordError("context_record_mismatch")
        return record

    async def write_context(self, *, owner_hash: str, record: RemoteA2AContextRecord) -> None:
        await write_json_atomic(
            self._context_path(
                owner_hash=owner_hash, connector_id=record.connector_id, context_id=record.context_id
            ),
            record.to_dict(),
        )


__all__ = [
    "BundleStorageRemoteA2AConnectorStore",
    "CONNECTORS_DIRNAME",
    "RemoteA2AConnectorStore",
    "RemoteA2AStorageError",
    "owner_hash_for",
    "task_key",
]

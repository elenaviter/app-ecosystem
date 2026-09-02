# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Immutable connector revisions under a shared bundle-storage root."""

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
from connection_hub.remote_mcp.models import (
    RemoteMCPConnector,
    RemoteMCPCurrentPointer,
    RemoteMCPRecordError,
    validated_connector_id,
)

CONNECTORS_DIRNAME = "remote-mcp-connectors"
CONNECTORS_LAYOUT_VERSION = "v1"
OWNERS_DIRNAME = "owners"
CONNECTORS_SUBDIRNAME = "connectors"
REVISIONS_DIRNAME = "revisions"
CURRENT_FILENAME = "current.json"

_OWNER_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_NAME_PATTERN = re.compile(
    r"^connector_revision_[0-9]{10}_[0-9]{8}_[0-9a-f]{12}\.json$"
)


class RemoteMCPStorageError(DurableStorageError):
    """Durable connector storage could not be read or written."""


def owner_hash_for(owner_subject: str) -> str:
    return hashlib.sha256(str(owner_subject or "").encode("utf-8")).hexdigest()


def validated_owner_hash(value: str) -> str:
    owner_hash = str(value or "").strip().lower()
    if not _OWNER_HASH_PATTERN.fullmatch(owner_hash):
        raise RemoteMCPStorageError("owner_hash_invalid")
    return owner_hash


def validated_revision_name(value: str) -> str:
    name = str(value or "").strip()
    if not _REVISION_NAME_PATTERN.fullmatch(name):
        raise RemoteMCPStorageError("revision_name_invalid")
    return name


class RemoteMCPConnectorStore(Protocol):
    def connector_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path: ...

    async def read_current_connector(
        self, *, owner_hash: str, connector_id: str
    ) -> tuple[RemoteMCPCurrentPointer, RemoteMCPConnector] | None: ...

    async def write_revision(
        self, *, owner_hash: str, connector: RemoteMCPConnector
    ) -> RemoteMCPCurrentPointer: ...

    async def advance_current(
        self, *, owner_hash: str, pointer: RemoteMCPCurrentPointer
    ) -> None: ...

    async def list_connector_ids(self, *, owner_hash: str) -> list[str]: ...


class BundleStorageRemoteMCPConnectorStore:
    def __init__(self, storage_root: str | os.PathLike[str]) -> None:
        self._root = (
            pathlib.Path(storage_root) / CONNECTORS_DIRNAME / CONNECTORS_LAYOUT_VERSION
        )

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def owner_path(self, owner_hash: str) -> pathlib.Path:
        return (
            self._root
            / OWNERS_DIRNAME
            / validated_owner_hash(owner_hash)
            / CONNECTORS_SUBDIRNAME
        )

    def connector_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path:
        return self.owner_path(owner_hash) / validated_connector_id(connector_id)

    def current_path(self, *, owner_hash: str, connector_id: str) -> pathlib.Path:
        return self.connector_path(
            owner_hash=owner_hash, connector_id=connector_id
        ) / CURRENT_FILENAME

    def revision_path(
        self, *, owner_hash: str, connector_id: str, revision_name: str
    ) -> pathlib.Path:
        return (
            self.connector_path(owner_hash=owner_hash, connector_id=connector_id)
            / REVISIONS_DIRNAME
            / validated_revision_name(revision_name)
        )

    async def read_current_connector(
        self, *, owner_hash: str, connector_id: str
    ) -> tuple[RemoteMCPCurrentPointer, RemoteMCPConnector] | None:
        pointer_payload = await read_json_or_none(
            self.current_path(owner_hash=owner_hash, connector_id=connector_id)
        )
        if pointer_payload is None:
            return None
        pointer = RemoteMCPCurrentPointer.from_mapping(pointer_payload)
        payload = await read_json_or_none(
            self.revision_path(
                owner_hash=owner_hash,
                connector_id=connector_id,
                revision_name=pointer.revision_name,
            )
        )
        if payload is None:
            raise RemoteMCPStorageError("current_revision_missing")
        connector = RemoteMCPConnector.from_mapping(payload)
        if connector.content_hash() != pointer.content_hash:
            raise RemoteMCPRecordError("revision_content_hash_mismatch")
        if connector.revision != pointer.revision:
            raise RemoteMCPRecordError("revision_number_mismatch")
        return pointer, connector

    async def write_revision(
        self, *, owner_hash: str, connector: RemoteMCPConnector
    ) -> RemoteMCPCurrentPointer:
        content_hash = connector.content_hash()
        revision_name = (
            f"connector_revision_{int(connector.updated_at):010d}_"
            f"{int(connector.revision):08d}_{content_hash[:12]}.json"
        )
        pointer = RemoteMCPCurrentPointer(
            connector_id=connector.connector_id,
            revision=connector.revision,
            revision_name=revision_name,
            content_hash=content_hash,
            updated_at=connector.updated_at,
        )
        await write_json_atomic(
            self.revision_path(
                owner_hash=owner_hash,
                connector_id=connector.connector_id,
                revision_name=revision_name,
            ),
            connector.to_dict(),
        )
        return pointer

    async def advance_current(
        self, *, owner_hash: str, pointer: RemoteMCPCurrentPointer
    ) -> None:
        await write_json_atomic(
            self.current_path(
                owner_hash=owner_hash, connector_id=pointer.connector_id
            ),
            pointer.to_dict(),
        )

    async def list_connector_ids(self, *, owner_hash: str) -> list[str]:
        names = await list_child_names(self.owner_path(owner_hash))
        out: list[str] = []
        for name in names:
            try:
                out.append(validated_connector_id(name))
            except RemoteMCPRecordError:
                continue
        return out


__all__ = [
    "CONNECTORS_DIRNAME",
    "CONNECTORS_LAYOUT_VERSION",
    "CURRENT_FILENAME",
    "BundleStorageRemoteMCPConnectorStore",
    "RemoteMCPConnectorStore",
    "RemoteMCPStorageError",
    "owner_hash_for",
    "validated_owner_hash",
]

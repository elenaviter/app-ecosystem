# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Connector lifecycle over host-provided storage, secrets, locks, and MCP IO."""

from __future__ import annotations

import pathlib
import secrets
import time
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Any, Mapping, Protocol

from connection_hub.remote_mcp.models import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    AUTH_OAUTH,
    CONNECTOR_ACTIVE,
    CONNECTOR_DELETED,
    CONNECTOR_DISABLED,
    DESCRIPTOR_ACCEPTED,
    DESCRIPTOR_DRIFTED,
    RemoteMCPConnector,
    RemoteMCPCredential,
    RemoteMCPDiscovery,
    RemoteMCPOAuthCredential,
    RemoteMCPRecordError,
    connector_resource,
    descriptor_drift,
    validated_auth_header,
    validated_connector_id,
)
from connection_hub.remote_mcp.security import RemoteMCPEndpointPolicy
from connection_hub.remote_mcp.store import (
    RemoteMCPConnectorStore,
    owner_hash_for,
)

CONNECTOR_LOCK_FILENAME = ".mutation.lock"
CONNECTOR_LOCK_WAIT_SECONDS = 30.0


class RemoteMCPConnectorConflict(RuntimeError):
    def __init__(self, reason: str, *, current_revision: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_revision = current_revision


class RemoteMCPConnectorNotFound(LookupError):
    def __init__(self, reason: str = "connector_not_found") -> None:
        super().__init__(reason)
        self.reason = reason


class RemoteMCPMutationLock(Protocol):
    def __call__(
        self,
        *,
        lock_path: pathlib.Path,
        resource_id: str,
        operation: str,
        wait_seconds: float,
    ) -> AbstractAsyncContextManager[Any]: ...


class RemoteMCPSecretStore(Protocol):
    async def set(
        self, *, owner_subject: str, secret_ref: str, value: str
    ) -> None: ...

    async def get(self, *, owner_subject: str, secret_ref: str) -> str | None: ...

    async def delete(self, *, owner_subject: str, secret_ref: str) -> None: ...


class RemoteMCPTransport(Protocol):
    async def discover(
        self,
        *,
        connector_id: str,
        endpoint: str,
        transport: str,
        headers: Mapping[str, str],
    ) -> RemoteMCPDiscovery: ...

    async def call_tool(
        self,
        *,
        connector_id: str,
        endpoint: str,
        transport: str,
        headers: Mapping[str, str],
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any: ...


def _credential_config(
    *, mode: Any, header: Any, value: Any
) -> tuple[str, str, RemoteMCPCredential]:
    normalized_mode = str(mode or AUTH_NONE).strip().lower() or AUTH_NONE
    if normalized_mode not in {AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_OAUTH}:
        raise RemoteMCPRecordError("credential_mode_invalid")
    normalized_header = ""
    secret = str(value or "")
    if normalized_mode == AUTH_NONE:
        if secret:
            raise RemoteMCPRecordError("credential_value_unexpected")
    else:
        if not secret:
            raise RemoteMCPRecordError("credential_value_missing")
        max_secret_size = 262144 if normalized_mode == AUTH_OAUTH else 16384
        if len(secret) > max_secret_size:
            raise RemoteMCPRecordError("credential_value_too_large")
    if normalized_mode == AUTH_HEADER:
        normalized_header = validated_auth_header(header)
    credential = RemoteMCPCredential(
        mode=normalized_mode,
        header=normalized_header,
        value=secret,
    )
    if normalized_mode == AUTH_OAUTH:
        RemoteMCPOAuthCredential.from_json(secret)
    credential.request_headers()
    return normalized_mode, normalized_header, credential


class RemoteMCPConnectorService:
    def __init__(
        self,
        *,
        store: RemoteMCPConnectorStore,
        secret_store: RemoteMCPSecretStore,
        transport: RemoteMCPTransport,
        endpoint_policy: RemoteMCPEndpointPolicy,
        mutation_lock: RemoteMCPMutationLock,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._transport = transport
        self._endpoint_policy = endpoint_policy
        self._mutation_lock = mutation_lock

    def _critical_section(self, *, owner_hash: str, connector_id: str):
        return self._mutation_lock(
            lock_path=(
                self._store.connector_path(
                    owner_hash=owner_hash, connector_id=connector_id
                )
                / CONNECTOR_LOCK_FILENAME
            ),
            resource_id=f"remote-mcp-connector:{connector_id}",
            operation="remote-mcp-connector-mutation",
            wait_seconds=CONNECTOR_LOCK_WAIT_SECONDS,
        )

    async def _transport_headers(
        self,
        *,
        connector_id: str,
        credential: RemoteMCPCredential,
        owner_subject: str,
        credential_ref: str,
    ) -> dict[str, str]:
        prepare = getattr(self._transport, "prepare_headers", None)
        if not callable(prepare):
            return credential.request_headers()
        headers = await prepare(
            connector_id=connector_id,
            credential=credential,
            owner_subject=owner_subject,
            credential_ref=credential_ref,
        )
        if not isinstance(headers, Mapping):
            raise RemoteMCPRecordError("transport_headers_invalid")
        return {str(key): str(value) for key, value in headers.items()}

    async def _current(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        include_deleted: bool = False,
    ) -> RemoteMCPConnector:
        loaded = await self._store.read_current_connector(
            owner_hash=owner_hash_for(owner_subject),
            connector_id=validated_connector_id(connector_id),
        )
        if loaded is None:
            raise RemoteMCPConnectorNotFound()
        connector = loaded[1]
        if connector.owner_subject != str(owner_subject or "").strip():
            raise RemoteMCPConnectorNotFound()
        if connector.state == CONNECTOR_DELETED and not include_deleted:
            raise RemoteMCPConnectorNotFound()
        return connector

    async def get(
        self, *, owner_subject: str, connector_id: str
    ) -> RemoteMCPConnector:
        return await self._current(
            owner_subject=owner_subject, connector_id=connector_id
        )

    async def list(self, *, owner_subject: str) -> list[RemoteMCPConnector]:
        owner = str(owner_subject or "").strip()
        if not owner:
            return []
        owner_hash = owner_hash_for(owner)
        records: list[RemoteMCPConnector] = []
        for connector_id in await self._store.list_connector_ids(owner_hash=owner_hash):
            try:
                record = await self._current(
                    owner_subject=owner, connector_id=connector_id
                )
            except RemoteMCPConnectorNotFound:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.label.lower(), item.connector_id))
        return records

    async def resolve_credential(
        self, *, owner_subject: str, connector_id: str
    ) -> RemoteMCPCredential:
        """Resolve one owner connector's secret for a trusted host adapter."""

        connector = await self._current(
            owner_subject=owner_subject,
            connector_id=connector_id,
        )
        return await self._credential(connector)

    async def create(
        self,
        *,
        owner_subject: str,
        label: str,
        endpoint: str,
        credential_mode: str = AUTH_NONE,
        credential_header: str = "",
        credential_value: str = "",
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        clean_label = str(label or "").strip()
        if not owner:
            raise RemoteMCPRecordError("owner_subject_missing")
        if not clean_label or len(clean_label) > 160:
            raise RemoteMCPRecordError("connector_label_invalid")
        connector_id = f"mcp_{secrets.token_hex(12)}"
        canonical_endpoint = await self._endpoint_policy.validate(endpoint)
        mode, header, credential = _credential_config(
            mode=credential_mode,
            header=credential_header,
            value=credential_value,
        )
        moment = int(now if now is not None else time.time())
        secret_ref = (
            f"remote_mcp.{connector_id}.credential.{uuid.uuid4().hex}"
            if mode != AUTH_NONE
            else ""
        )
        connector = RemoteMCPConnector(
            connector_id=connector_id,
            owner_subject=owner,
            label=clean_label,
            endpoint=canonical_endpoint,
            transport="streamable-http",
            resource=connector_resource(connector_id),
            revision=1,
            state=CONNECTOR_ACTIVE,
            credential_mode=mode,
            credential_header=header,
            credential_ref=secret_ref,
            tools=(),
            descriptor_digest="",
            descriptor_revision=1,
            descriptor_state=DESCRIPTOR_ACCEPTED,
            server_name="",
            server_version="",
            protocol_version="",
            created_at=moment,
            updated_at=moment,
            last_checked_at=moment,
        )
        owner_hash = owner_hash_for(owner)
        if secret_ref:
            await self._secret_store.set(
                owner_subject=owner,
                secret_ref=secret_ref,
                value=credential.value,
            )
        try:
            headers = await self._transport_headers(
                connector_id=connector_id,
                credential=credential,
                owner_subject=owner,
                credential_ref=secret_ref,
            )
            discovery = await self._transport.discover(
                connector_id=connector_id,
                endpoint=canonical_endpoint,
                transport="streamable-http",
                headers=headers,
            )
            connector = replace(
                connector,
                tools=discovery.tools,
                descriptor_digest=discovery.descriptor_digest,
                server_name=discovery.server_name,
                server_version=discovery.server_version,
                protocol_version=discovery.protocol_version,
            )
            connector.verify()
            async with self._critical_section(
                owner_hash=owner_hash, connector_id=connector_id
            ):
                existing = await self._store.read_current_connector(
                    owner_hash=owner_hash, connector_id=connector_id
                )
                if existing is not None:
                    raise RemoteMCPConnectorConflict(
                        "connector_already_exists",
                        current_revision=existing[1].revision,
                    )
                await self._commit(owner_hash=owner_hash, connector=connector)
        except Exception:
            if secret_ref:
                await self._best_effort_revoke(
                    connector_id=connector.connector_id,
                    endpoint=connector.endpoint,
                    transport=connector.transport,
                    owner_subject=connector.owner_subject,
                    credential=credential,
                    credential_ref=secret_ref,
                )
                await self._secret_store.delete(
                    owner_subject=owner, secret_ref=secret_ref
                )
            raise
        return connector

    async def refresh(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        expected_revision: int,
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(
            owner_hash=owner_hash, connector_id=connector_id
        ):
            current = await self._expect_current(
                owner_subject=owner,
                connector_id=connector_id,
                expected_revision=expected_revision,
            )
            credential = await self._credential(current)
            endpoint = await self._endpoint_policy.validate(current.endpoint)
            headers = await self._transport_headers(
                connector_id=current.connector_id,
                credential=credential,
                owner_subject=current.owner_subject,
                credential_ref=current.credential_ref,
            )
            discovery = await self._transport.discover(
                connector_id=current.connector_id,
                endpoint=endpoint,
                transport=current.transport,
                headers=headers,
            )
            moment = int(now if now is not None else time.time())
            if discovery.descriptor_digest == current.descriptor_digest:
                updated = replace(
                    current,
                    revision=current.revision + 1,
                    descriptor_state=DESCRIPTOR_ACCEPTED,
                    pending_tools=(),
                    pending_descriptor_digest="",
                    drift={},
                    server_name=discovery.server_name,
                    server_version=discovery.server_version,
                    protocol_version=discovery.protocol_version,
                    updated_at=moment,
                    last_checked_at=moment,
                    last_error="",
                )
            else:
                drift = descriptor_drift(current.tools, discovery.tools)
                updated = replace(
                    current,
                    revision=current.revision + 1,
                    descriptor_state=DESCRIPTOR_DRIFTED,
                    pending_tools=discovery.tools,
                    pending_descriptor_digest=discovery.descriptor_digest,
                    drift={key: tuple(values) for key, values in drift.items()},
                    server_name=discovery.server_name,
                    server_version=discovery.server_version,
                    protocol_version=discovery.protocol_version,
                    updated_at=moment,
                    last_checked_at=moment,
                    last_error="",
                )
            updated.verify()
            await self._commit(owner_hash=owner_hash, connector=updated)
            return updated

    async def accept_descriptor(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        expected_revision: int,
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(
            owner_hash=owner_hash, connector_id=connector_id
        ):
            current = await self._expect_current(
                owner_subject=owner,
                connector_id=connector_id,
                expected_revision=expected_revision,
            )
            if current.descriptor_state != DESCRIPTOR_DRIFTED:
                return current
            moment = int(now if now is not None else time.time())
            updated = replace(
                current,
                revision=current.revision + 1,
                tools=current.pending_tools,
                descriptor_digest=current.pending_descriptor_digest,
                descriptor_revision=current.descriptor_revision + 1,
                descriptor_state=DESCRIPTOR_ACCEPTED,
                pending_tools=(),
                pending_descriptor_digest="",
                drift={},
                updated_at=moment,
                last_checked_at=moment,
                last_error="",
            )
            updated.verify()
            await self._commit(owner_hash=owner_hash, connector=updated)
            return updated

    async def set_enabled(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        enabled: bool,
        expected_revision: int,
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(
            owner_hash=owner_hash, connector_id=connector_id
        ):
            current = await self._expect_current(
                owner_subject=owner,
                connector_id=connector_id,
                expected_revision=expected_revision,
            )
            updated = replace(
                current,
                revision=current.revision + 1,
                state=CONNECTOR_ACTIVE if enabled else CONNECTOR_DISABLED,
                updated_at=int(now if now is not None else time.time()),
            )
            updated.verify()
            await self._commit(owner_hash=owner_hash, connector=updated)
            return updated

    async def replace_credential(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        expected_revision: int,
        credential_mode: str,
        credential_header: str = "",
        credential_value: str = "",
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        mode, header, credential = _credential_config(
            mode=credential_mode,
            header=credential_header,
            value=credential_value,
        )
        new_ref = (
            f"remote_mcp.{connector_id}.credential.{uuid.uuid4().hex}"
            if mode != AUTH_NONE
            else ""
        )
        old_ref = ""
        old_credential: RemoteMCPCredential | None = None
        current: RemoteMCPConnector | None = None
        if new_ref:
            await self._secret_store.set(
                owner_subject=owner, secret_ref=new_ref, value=credential.value
            )
        try:
            async with self._critical_section(
                owner_hash=owner_hash, connector_id=connector_id
            ):
                current = await self._expect_current(
                    owner_subject=owner,
                    connector_id=connector_id,
                    expected_revision=expected_revision,
                )
                old_ref = current.credential_ref
                old_credential = await self._credential(current)
                endpoint = await self._endpoint_policy.validate(current.endpoint)
                headers = await self._transport_headers(
                    connector_id=current.connector_id,
                    credential=credential,
                    owner_subject=current.owner_subject,
                    credential_ref=new_ref,
                )
                discovery = await self._transport.discover(
                    connector_id=current.connector_id,
                    endpoint=endpoint,
                    transport=current.transport,
                    headers=headers,
                )
                moment = int(now if now is not None else time.time())
                same_descriptor = discovery.descriptor_digest == current.descriptor_digest
                updated = replace(
                    current,
                    revision=current.revision + 1,
                    credential_mode=mode,
                    credential_header=header,
                    credential_ref=new_ref,
                    descriptor_state=(
                        DESCRIPTOR_ACCEPTED if same_descriptor else DESCRIPTOR_DRIFTED
                    ),
                    pending_tools=() if same_descriptor else discovery.tools,
                    pending_descriptor_digest=(
                        "" if same_descriptor else discovery.descriptor_digest
                    ),
                    drift=(
                        {}
                        if same_descriptor
                        else {
                            key: tuple(values)
                            for key, values in descriptor_drift(
                                current.tools, discovery.tools
                            ).items()
                        }
                    ),
                    server_name=discovery.server_name,
                    server_version=discovery.server_version,
                    protocol_version=discovery.protocol_version,
                    updated_at=moment,
                    last_checked_at=moment,
                    last_error="",
                )
                updated.verify()
                await self._commit(owner_hash=owner_hash, connector=updated)
        except Exception:
            if new_ref:
                if current is not None:
                    await self._best_effort_revoke(
                        connector_id=connector_id,
                        endpoint=current.endpoint,
                        transport=current.transport,
                        owner_subject=owner,
                        credential=credential,
                        credential_ref=new_ref,
                    )
                else:
                    await self._best_effort_revoke(
                        connector_id=connector_id,
                        endpoint="",
                        transport="streamable-http",
                        owner_subject=owner,
                        credential=credential,
                        credential_ref=new_ref,
                    )
                await self._secret_store.delete(
                    owner_subject=owner, secret_ref=new_ref
                )
            raise
        if old_ref and old_ref != new_ref:
            if current is not None and old_credential is not None:
                await self._best_effort_revoke(
                    connector_id=current.connector_id,
                    endpoint=current.endpoint,
                    transport=current.transport,
                    owner_subject=current.owner_subject,
                    credential=old_credential,
                    credential_ref=old_ref,
                )
            await self._secret_store.delete(owner_subject=owner, secret_ref=old_ref)
        return updated

    async def delete(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        expected_revision: int,
        now: int | None = None,
    ) -> RemoteMCPConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        old_ref = ""
        async with self._critical_section(
            owner_hash=owner_hash, connector_id=connector_id
        ):
            current = await self._expect_current(
                owner_subject=owner,
                connector_id=connector_id,
                expected_revision=expected_revision,
            )
            old_ref = current.credential_ref
            credential = await self._credential(current)
            await self._best_effort_revoke(
                connector_id=current.connector_id,
                endpoint=current.endpoint,
                transport=current.transport,
                owner_subject=current.owner_subject,
                credential=credential,
                credential_ref=current.credential_ref,
            )
            deleted = replace(
                current,
                revision=current.revision + 1,
                state=CONNECTOR_DELETED,
                credential_mode=AUTH_NONE,
                credential_header="",
                credential_ref="",
                updated_at=int(now if now is not None else time.time()),
            )
            deleted.verify()
            await self._commit(owner_hash=owner_hash, connector=deleted)
        if old_ref:
            await self._secret_store.delete(owner_subject=owner, secret_ref=old_ref)
        return deleted

    async def observe(
        self, connector: RemoteMCPConnector
    ) -> RemoteMCPDiscovery:
        if connector.state != CONNECTOR_ACTIVE:
            raise RemoteMCPConnectorConflict("connector_not_active")
        endpoint = await self._endpoint_policy.validate(connector.endpoint)
        credential = await self._credential(connector)
        headers = await self._transport_headers(
            connector_id=connector.connector_id,
            credential=credential,
            owner_subject=connector.owner_subject,
            credential_ref=connector.credential_ref,
        )
        return await self._transport.discover(
            connector_id=connector.connector_id,
            endpoint=endpoint,
            transport=connector.transport,
            headers=headers,
        )

    async def call_tool(
        self,
        *,
        connector: RemoteMCPConnector,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        if connector.state != CONNECTOR_ACTIVE:
            raise RemoteMCPConnectorConflict("connector_not_active")
        endpoint = await self._endpoint_policy.validate(connector.endpoint)
        credential = await self._credential(connector)
        headers = await self._transport_headers(
            connector_id=connector.connector_id,
            credential=credential,
            owner_subject=connector.owner_subject,
            credential_ref=connector.credential_ref,
        )
        return await self._transport.call_tool(
            connector_id=connector.connector_id,
            endpoint=endpoint,
            transport=connector.transport,
            headers=headers,
            tool_name=str(tool_name or "").strip(),
            arguments=dict(arguments or {}),
        )

    async def _credential(
        self, connector: RemoteMCPConnector
    ) -> RemoteMCPCredential:
        if connector.credential_mode == AUTH_NONE:
            return RemoteMCPCredential()
        value = await self._secret_store.get(
            owner_subject=connector.owner_subject,
            secret_ref=connector.credential_ref,
        )
        if not value:
            raise RemoteMCPConnectorConflict("connector_credential_unavailable")
        return RemoteMCPCredential(
            mode=connector.credential_mode,
            header=connector.credential_header,
            value=value,
        )

    async def _best_effort_revoke(
        self,
        *,
        connector_id: str,
        endpoint: str,
        transport: str,
        owner_subject: str,
        credential: RemoteMCPCredential,
        credential_ref: str,
    ) -> None:
        revoke = getattr(self._transport, "revoke_credential", None)
        if not callable(revoke):
            return
        try:
            await revoke(
                connector_id=connector_id,
                endpoint=endpoint,
                transport=transport,
                credential=credential,
                owner_subject=owner_subject,
                credential_ref=credential_ref,
            )
        except Exception:
            # The local secret/card transition remains authoritative. Failure
            # of an optional provider revocation endpoint cannot keep local
            # authority alive or make a failed connector visible.
            pass

    async def _expect_current(
        self,
        *,
        owner_subject: str,
        connector_id: str,
        expected_revision: int,
    ) -> RemoteMCPConnector:
        current = await self._current(
            owner_subject=owner_subject,
            connector_id=connector_id,
            include_deleted=True,
        )
        if current.state == CONNECTOR_DELETED:
            raise RemoteMCPConnectorNotFound()
        if current.revision != int(expected_revision):
            raise RemoteMCPConnectorConflict(
                "connector_revision_moved", current_revision=current.revision
            )
        return current

    async def _commit(
        self, *, owner_hash: str, connector: RemoteMCPConnector
    ) -> None:
        pointer = await self._store.write_revision(
            owner_hash=owner_hash, connector=connector
        )
        await self._store.advance_current(owner_hash=owner_hash, pointer=pointer)


__all__ = [
    "CONNECTOR_LOCK_FILENAME",
    "CONNECTOR_LOCK_WAIT_SECONDS",
    "RemoteMCPConnectorConflict",
    "RemoteMCPConnectorNotFound",
    "RemoteMCPConnectorService",
    "RemoteMCPMutationLock",
    "RemoteMCPSecretStore",
    "RemoteMCPTransport",
]

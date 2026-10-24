# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A2A connector lifecycle and task calls over host storage, secrets and locks.

The protocol work (card fetch and pinning, the outbound endpoint policy,
JSON-RPC calls, fixed failure codes) is ``app_foundation.a2a``. This service
owns what is Connection Hub's: the owner's connector registry, the
Hub-held credential, drift suspension, and the rule that only the Card that
created a remote task may read, cancel or answer it.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import replace
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from app_foundation.a2a import A2ATask, PinnedAgentCard

from connection_hub.remote_a2a.models import (
    A2A_AUTH_MODES,
    RemoteA2AContextRecord,
    RemoteA2AConnector,
    RemoteA2ARecordError,
    RemoteA2ATaskRecord,
    connector_resource,
    validated_connector_id,
)
from connection_hub.remote_a2a.store import RemoteA2AConnectorStore, owner_hash_for
from connection_hub.remote_mcp.models import (
    AUTH_HEADER,
    AUTH_NONE,
    CONNECTOR_ACTIVE,
    CONNECTOR_DELETED,
    CONNECTOR_DISABLED,
    DESCRIPTOR_ACCEPTED,
    DESCRIPTOR_DRIFTED,
    RemoteMCPCredential,
    validated_auth_header,
)
from connection_hub.remote_mcp.service import (
    RemoteMCPConnectorConflict as RemoteA2AConnectorConflict,
    RemoteMCPConnectorNotFound as RemoteA2AConnectorNotFound,
    RemoteMCPMutationLock,
    RemoteMCPSecretStore,
)

CONNECTOR_LOCK_FILENAME = ".mutation.lock"
CONNECTOR_LOCK_WAIT_SECONDS = 30.0
MAX_CREDENTIAL_CHARS = 16384


class RemoteA2ATaskNotFound(LookupError):
    """No task with this id was created by this Card through this connector."""

    def __init__(self) -> None:
        super().__init__("a2a_task_not_found")
        self.reason = "a2a_task_not_found"


class RemoteA2AContextNotFound(LookupError):
    """This Card did not start a remote context with this id through this connector."""

    def __init__(self) -> None:
        super().__init__("a2a_context_not_found")
        self.reason = "a2a_context_not_found"


class A2AClientPort(Protocol):
    async def fetch_card(self, card_url: str) -> PinnedAgentCard: ...

    async def send_message(
        self,
        card: PinnedAgentCard,
        *,
        headers: Mapping[str, str],
        text: str,
        data: Mapping[str, Any] | None = None,
        context_id: str = "",
        task_id: str = "",
        message_id: str = "",
    ) -> A2ATask: ...

    async def get_task(self, card: PinnedAgentCard, *, headers: Mapping[str, str], task_id: str) -> A2ATask: ...

    async def cancel_task(self, card: PinnedAgentCard, *, headers: Mapping[str, str], task_id: str) -> A2ATask: ...


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _credential_config(*, mode: Any, header: Any, value: Any) -> tuple[str, str, RemoteMCPCredential]:
    normalized_mode = str(mode or AUTH_NONE).strip().lower() or AUTH_NONE
    if normalized_mode not in A2A_AUTH_MODES:
        # Outbound OAuth to an A2A agent is a later phase; it is refused here.
        raise RemoteA2ARecordError("credential_mode_invalid")
    secret = str(value or "")
    if normalized_mode == AUTH_NONE:
        if secret:
            raise RemoteA2ARecordError("credential_value_unexpected")
    elif not secret:
        raise RemoteA2ARecordError("credential_value_missing")
    elif len(secret) > MAX_CREDENTIAL_CHARS:
        raise RemoteA2ARecordError("credential_value_too_large")
    normalized_header = validated_auth_header(header) if normalized_mode == AUTH_HEADER else ""
    credential = RemoteMCPCredential(mode=normalized_mode, header=normalized_header, value=secret)
    credential.request_headers()
    return normalized_mode, normalized_header, credential


class RemoteA2AConnectorService:
    def __init__(
        self,
        *,
        store: RemoteA2AConnectorStore,
        secret_store: RemoteMCPSecretStore,
        client: A2AClientPort,
        mutation_lock: RemoteMCPMutationLock,
        allow_cross_origin_endpoint: bool = False,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._client = client
        self._mutation_lock = mutation_lock
        self._allow_cross_origin_endpoint = bool(allow_cross_origin_endpoint)

    def _critical_section(self, *, owner_hash: str, connector_id: str):
        return self._mutation_lock(
            lock_path=self._store.connector_path(owner_hash=owner_hash, connector_id=connector_id)
            / CONNECTOR_LOCK_FILENAME,
            resource_id=f"remote-a2a-connector:{connector_id}",
            operation="remote-a2a-connector-mutation",
            wait_seconds=CONNECTOR_LOCK_WAIT_SECONDS,
        )

    async def _fetch_card(self, card_url: str) -> PinnedAgentCard:
        card = await self._client.fetch_card(card_url)
        if not self._allow_cross_origin_endpoint and _origin(card.jsonrpc_url) != _origin(card.card_url):
            # The credential goes to the card's endpoint, so an endpoint on another
            # origin than the card the owner named is refused. Only a deployment
            # that sets allow_cross_origin_endpoint accepts one.
            raise RemoteA2ARecordError("card_endpoint_cross_origin")
        return card

    async def _current(self, *, owner_subject: str, connector_id: str, include_deleted: bool = False) -> RemoteA2AConnector:
        loaded = await self._store.read_current_connector(
            owner_hash=owner_hash_for(owner_subject),
            connector_id=validated_connector_id(connector_id),
        )
        if loaded is None:
            raise RemoteA2AConnectorNotFound()
        connector = loaded[1]
        if connector.owner_subject != str(owner_subject or "").strip():
            raise RemoteA2AConnectorNotFound()
        if connector.state == CONNECTOR_DELETED and not include_deleted:
            raise RemoteA2AConnectorNotFound()
        return connector

    async def get(self, *, owner_subject: str, connector_id: str) -> RemoteA2AConnector:
        return await self._current(owner_subject=owner_subject, connector_id=connector_id)

    async def list(self, *, owner_subject: str) -> list[RemoteA2AConnector]:
        owner = str(owner_subject or "").strip()
        if not owner:
            return []
        records: list[RemoteA2AConnector] = []
        for connector_id in await self._store.list_connector_ids(owner_hash=owner_hash_for(owner)):
            try:
                records.append(await self._current(owner_subject=owner, connector_id=connector_id))
            except RemoteA2AConnectorNotFound:
                continue
        records.sort(key=lambda item: (item.label.lower(), item.connector_id))
        return records

    async def create(
        self,
        *,
        owner_subject: str,
        label: str,
        card_url: str,
        credential_mode: str = AUTH_NONE,
        credential_header: str = "",
        credential_value: str = "",
        now: int | None = None,
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        clean_label = str(label or "").strip()
        if not owner:
            raise RemoteA2ARecordError("owner_subject_missing")
        if not clean_label or len(clean_label) > 160:
            raise RemoteA2ARecordError("connector_label_invalid")
        mode, header, credential = _credential_config(
            mode=credential_mode, header=credential_header, value=credential_value
        )
        card = await self._fetch_card(card_url)
        connector_id = f"a2a_{secrets.token_hex(12)}"
        moment = int(now if now is not None else time.time())
        secret_ref = f"remote_a2a.{connector_id}.credential.{uuid.uuid4().hex}" if mode != AUTH_NONE else ""
        connector = RemoteA2AConnector(
            connector_id=connector_id,
            owner_subject=owner,
            label=clean_label,
            card_url=card.card_url,
            resource=connector_resource(connector_id),
            revision=1,
            state=CONNECTOR_ACTIVE,
            credential_mode=mode,
            credential_header=header,
            credential_ref=secret_ref,
            card=card,
            created_at=moment,
            updated_at=moment,
            last_checked_at=moment,
        )
        connector.verify()
        owner_hash = owner_hash_for(owner)
        if secret_ref:
            await self._secret_store.set(owner_subject=owner, secret_ref=secret_ref, value=credential.value)
        try:
            async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
                existing = await self._store.read_current_connector(owner_hash=owner_hash, connector_id=connector_id)
                if existing is not None:
                    raise RemoteA2AConnectorConflict("connector_already_exists", current_revision=existing[1].revision)
                await self._commit(owner_hash=owner_hash, connector=connector)
        except Exception:
            if secret_ref:
                await self._secret_store.delete(owner_subject=owner, secret_ref=secret_ref)
            raise
        return connector

    async def refresh(
        self, *, owner_subject: str, connector_id: str, expected_revision: int, now: int | None = None
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
            current = await self._expect_current(
                owner_subject=owner, connector_id=connector_id, expected_revision=expected_revision
            )
            observed = await self._fetch_card(current.card_url)
            moment = int(now if now is not None else time.time())
            same = current.card is not None and observed.digest == current.card.digest
            updated = replace(
                current,
                revision=current.revision + 1,
                descriptor_state=DESCRIPTOR_ACCEPTED if same else DESCRIPTOR_DRIFTED,
                pending_card=None if same else observed,
                updated_at=moment,
                last_checked_at=moment,
                last_error="",
            )
            updated.verify()
            await self._commit(owner_hash=owner_hash, connector=updated)
            return updated

    async def accept_descriptor(
        self, *, owner_subject: str, connector_id: str, expected_revision: int, now: int | None = None
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
            current = await self._expect_current(
                owner_subject=owner, connector_id=connector_id, expected_revision=expected_revision
            )
            if current.descriptor_state != DESCRIPTOR_DRIFTED or current.pending_card is None:
                return current
            moment = int(now if now is not None else time.time())
            updated = replace(
                current,
                revision=current.revision + 1,
                card=current.pending_card,
                descriptor_revision=current.descriptor_revision + 1,
                descriptor_state=DESCRIPTOR_ACCEPTED,
                pending_card=None,
                updated_at=moment,
                last_checked_at=moment,
                last_error="",
            )
            updated.verify()
            await self._commit(owner_hash=owner_hash, connector=updated)
            return updated

    async def set_enabled(
        self, *, owner_subject: str, connector_id: str, enabled: bool, expected_revision: int, now: int | None = None
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
            current = await self._expect_current(
                owner_subject=owner, connector_id=connector_id, expected_revision=expected_revision
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
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        mode, header, credential = _credential_config(
            mode=credential_mode, header=credential_header, value=credential_value
        )
        new_ref = f"remote_a2a.{connector_id}.credential.{uuid.uuid4().hex}" if mode != AUTH_NONE else ""
        old_ref = ""
        if new_ref:
            await self._secret_store.set(owner_subject=owner, secret_ref=new_ref, value=credential.value)
        try:
            async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
                current = await self._expect_current(
                    owner_subject=owner, connector_id=connector_id, expected_revision=expected_revision
                )
                old_ref = current.credential_ref
                updated = replace(
                    current,
                    revision=current.revision + 1,
                    credential_mode=mode,
                    credential_header=header,
                    credential_ref=new_ref,
                    updated_at=int(now if now is not None else time.time()),
                )
                updated.verify()
                await self._commit(owner_hash=owner_hash, connector=updated)
        except Exception:
            if new_ref:
                await self._secret_store.delete(owner_subject=owner, secret_ref=new_ref)
            raise
        if old_ref and old_ref != new_ref:
            await self._secret_store.delete(owner_subject=owner, secret_ref=old_ref)
        return updated

    async def delete(
        self, *, owner_subject: str, connector_id: str, expected_revision: int, now: int | None = None
    ) -> RemoteA2AConnector:
        owner = str(owner_subject or "").strip()
        connector_id = validated_connector_id(connector_id)
        owner_hash = owner_hash_for(owner)
        async with self._critical_section(owner_hash=owner_hash, connector_id=connector_id):
            current = await self._expect_current(
                owner_subject=owner, connector_id=connector_id, expected_revision=expected_revision
            )
            old_ref = current.credential_ref
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

    async def observe(self, connector: RemoteA2AConnector) -> PinnedAgentCard:
        """The live card, for the gateway's readiness and drift check."""

        if connector.state != CONNECTOR_ACTIVE:
            raise RemoteA2AConnectorConflict("connector_not_active")
        return await self._fetch_card(connector.card_url)

    async def _call_headers(self, connector: RemoteA2AConnector) -> dict[str, str]:
        if connector.state != CONNECTOR_ACTIVE or connector.card is None:
            raise RemoteA2AConnectorConflict("connector_not_active")
        if connector.credential_mode == AUTH_NONE:
            return {}
        value = await self._secret_store.get(owner_subject=connector.owner_subject, secret_ref=connector.credential_ref)
        if not value:
            raise RemoteA2AConnectorConflict("connector_credential_unavailable")
        return RemoteMCPCredential(
            mode=connector.credential_mode, header=connector.credential_header, value=value
        ).request_headers()

    async def task_owned_by(self, *, connector: RemoteA2AConnector, access_id: str, task_id: str) -> RemoteA2ATaskRecord:
        record = await self._store.read_task(
            owner_hash=owner_hash_for(connector.owner_subject),
            connector_id=connector.connector_id,
            task_id=str(task_id or "").strip(),
        ) if str(task_id or "").strip() else None
        if record is None or not access_id or record.access_id != access_id:
            raise RemoteA2ATaskNotFound()
        return record

    async def context_owned_by(self, *, connector: RemoteA2AConnector, access_id: str, context_id: str) -> None:
        """A non-empty context id continues only a context this Card started."""

        context = str(context_id or "").strip()
        if not context:
            return
        record = await self._store.read_context(
            owner_hash=owner_hash_for(connector.owner_subject),
            connector_id=connector.connector_id,
            context_id=context,
        )
        if record is None or not access_id or record.access_id != access_id:
            raise RemoteA2AContextNotFound()

    async def _record_sent(self, *, connector: RemoteA2AConnector, access_id: str, task: A2ATask, now: int | None) -> None:
        """Bind a new task and context to this Card. Create-only: a record that
        another Card already holds is never replaced (an outside agent that
        repeats ids cannot move a task or a context to another Card)."""

        owner_hash = owner_hash_for(connector.owner_subject)
        moment = int(now if now is not None else time.time())
        async with self._critical_section(owner_hash=owner_hash, connector_id=connector.connector_id):
            if task.task_id:
                existing = await self._store.read_task(
                    owner_hash=owner_hash, connector_id=connector.connector_id, task_id=task.task_id
                )
                if existing is None:
                    await self._store.write_task(
                        owner_hash=owner_hash,
                        record=RemoteA2ATaskRecord(
                            connector_id=connector.connector_id,
                            task_id=task.task_id,
                            access_id=access_id,
                            context_id=task.context_id,
                            created_at=moment,
                        ),
                    )
            if task.context_id:
                existing_context = await self._store.read_context(
                    owner_hash=owner_hash, connector_id=connector.connector_id, context_id=task.context_id
                )
                if existing_context is None:
                    await self._store.write_context(
                        owner_hash=owner_hash,
                        record=RemoteA2AContextRecord(
                            connector_id=connector.connector_id,
                            context_id=task.context_id,
                            access_id=access_id,
                            created_at=moment,
                        ),
                    )

    async def send(
        self,
        *,
        connector: RemoteA2AConnector,
        access_id: str,
        skill_id: str,
        text: str,
        data: Mapping[str, Any] | None = None,
        context_id: str = "",
        now: int | None = None,
    ) -> A2ATask:
        if connector.card is None or connector.card.skill(skill_id) is None:
            raise RemoteA2ARecordError("skill_not_on_card")
        if not access_id:
            raise RemoteA2ARecordError("access_id_missing")
        await self.context_owned_by(connector=connector, access_id=access_id, context_id=context_id)
        headers = await self._call_headers(connector)
        task = await self._client.send_message(
            connector.card, headers=headers, text=text, data=data, context_id=context_id
        )
        await self._record_sent(connector=connector, access_id=access_id, task=task, now=now)
        return task

    async def get_task(self, *, connector: RemoteA2AConnector, access_id: str, task_id: str) -> A2ATask:
        await self.task_owned_by(connector=connector, access_id=access_id, task_id=task_id)
        return await self._client.get_task(connector.card, headers=await self._call_headers(connector), task_id=task_id)

    async def cancel_task(self, *, connector: RemoteA2AConnector, access_id: str, task_id: str) -> A2ATask:
        await self.task_owned_by(connector=connector, access_id=access_id, task_id=task_id)
        return await self._client.cancel_task(connector.card, headers=await self._call_headers(connector), task_id=task_id)

    async def reply(
        self,
        *,
        connector: RemoteA2AConnector,
        access_id: str,
        task_id: str,
        text: str,
        data: Mapping[str, Any] | None = None,
    ) -> A2ATask:
        record = await self.task_owned_by(connector=connector, access_id=access_id, task_id=task_id)
        return await self._client.send_message(
            connector.card,
            headers=await self._call_headers(connector),
            text=text,
            data=data,
            context_id=record.context_id,
            task_id=record.task_id,
        )

    async def _expect_current(self, *, owner_subject: str, connector_id: str, expected_revision: int) -> RemoteA2AConnector:
        current = await self._current(owner_subject=owner_subject, connector_id=connector_id, include_deleted=True)
        if current.state == CONNECTOR_DELETED:
            raise RemoteA2AConnectorNotFound()
        if current.revision != int(expected_revision):
            raise RemoteA2AConnectorConflict("connector_revision_moved", current_revision=current.revision)
        return current

    async def _commit(self, *, owner_hash: str, connector: RemoteA2AConnector) -> None:
        pointer = await self._store.write_revision(owner_hash=owner_hash, connector=connector)
        await self._store.advance_current(owner_hash=owner_hash, pointer=pointer)


__all__ = [
    "A2AClientPort",
    "RemoteA2AContextNotFound",
    "RemoteA2AConnectorConflict",
    "RemoteA2AConnectorNotFound",
    "RemoteA2AConnectorService",
    "RemoteA2ATaskNotFound",
]

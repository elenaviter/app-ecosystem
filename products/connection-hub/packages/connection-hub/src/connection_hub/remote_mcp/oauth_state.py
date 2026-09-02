# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Single-use OAuth transactions for owner-configured remote MCP servers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.durable_io import (
    list_child_names,
    read_json_or_none,
    write_json_atomic,
)


class RemoteMCPSecretStore(Protocol):
    async def set(
        self, *, owner_subject: str, secret_ref: str, value: str
    ) -> None: ...

    async def get(self, *, owner_subject: str, secret_ref: str) -> str | None: ...

    async def delete(self, *, owner_subject: str, secret_ref: str) -> None: ...

OAUTH_STATE_POINTER_SCHEMA = "connection_hub.remote_mcp_oauth_state.v1"
OAUTH_STATE_SECRET_SCHEMA = "connection_hub.remote_mcp_oauth_transaction.v1"
OAUTH_STATE_DIRNAME = "remote-mcp-oauth"
OAUTH_STATE_LAYOUT_VERSION = "v1"
OAUTH_STATE_SUBDIRNAME = "states"
OAUTH_STATE_DEFAULT_TTL_SECONDS = 900
OAUTH_STATE_MAX_TTL_SECONDS = 3600


class RemoteMCPOAuthStateError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RemoteMCPOAuthStateHandle:
    state: str
    state_digest: str
    expires_at: int


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    return str(value or "").strip()


class BundleStorageRemoteMCPOAuthStateStore:
    """Durable state pointer plus user-secret transaction payload.

    The random callback state is never written to storage. Its digest locates a
    non-secret pointer. PKCE, dynamic client credentials, and endpoint metadata
    stay in the referenced user secret and are deleted when the state is claimed.
    """

    def __init__(
        self,
        storage_root: str | os.PathLike[str],
        *,
        secret_store: RemoteMCPSecretStore,
    ) -> None:
        self._root = (
            pathlib.Path(storage_root)
            / OAUTH_STATE_DIRNAME
            / OAUTH_STATE_LAYOUT_VERSION
            / OAUTH_STATE_SUBDIRNAME
        )
        self._secret_store = secret_store

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, state_digest: str) -> pathlib.Path:
        digest = _clean(state_digest).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RemoteMCPOAuthStateError("oauth_state_digest_invalid")
        return self._root / f"{digest}.json"

    async def create(
        self,
        *,
        owner_subject: str,
        transaction: Mapping[str, Any],
        ttl_seconds: int = OAUTH_STATE_DEFAULT_TTL_SECONDS,
        now: int | None = None,
    ) -> RemoteMCPOAuthStateHandle:
        owner = _clean(owner_subject)
        if not owner:
            raise RemoteMCPOAuthStateError("oauth_state_owner_missing")
        if not isinstance(transaction, Mapping):
            raise RemoteMCPOAuthStateError("oauth_transaction_invalid")
        try:
            transaction_payload = json.loads(
                json.dumps(
                    dict(transaction),
                    ensure_ascii=True,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise RemoteMCPOAuthStateError("oauth_transaction_not_json") from exc
        if not isinstance(transaction_payload, dict):
            raise RemoteMCPOAuthStateError("oauth_transaction_invalid")
        ttl = int(ttl_seconds or OAUTH_STATE_DEFAULT_TTL_SECONDS)
        if ttl < 60 or ttl > OAUTH_STATE_MAX_TTL_SECONDS:
            raise RemoteMCPOAuthStateError("oauth_state_ttl_invalid")
        moment = int(now if now is not None else time.time())
        state = secrets.token_urlsafe(48)
        digest = _state_digest(state)
        secret_ref = f"remote_mcp.oauth_state.{uuid.uuid4().hex}"
        expires_at = moment + ttl
        secret_payload = {
            "schema": OAUTH_STATE_SECRET_SCHEMA,
            "state_digest": digest,
            "transaction": transaction_payload,
            "created_at": moment,
            "expires_at": expires_at,
        }
        pointer = {
            "schema": OAUTH_STATE_POINTER_SCHEMA,
            "state_digest": digest,
            "owner_subject": owner,
            "secret_ref": secret_ref,
            "created_at": moment,
            "expires_at": expires_at,
        }
        await self._secret_store.set(
            owner_subject=owner,
            secret_ref=secret_ref,
            value=json.dumps(
                secret_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        try:
            await write_json_atomic(self._path(digest), pointer)
        except Exception:
            await self._secret_store.delete(
                owner_subject=owner, secret_ref=secret_ref
            )
            raise
        return RemoteMCPOAuthStateHandle(
            state=state,
            state_digest=digest,
            expires_at=expires_at,
        )

    async def consume(
        self,
        *,
        state: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        raw_state = _clean(state)
        if len(raw_state) < 43 or len(raw_state) > 512:
            raise RemoteMCPOAuthStateError("oauth_state_invalid")
        digest = _state_digest(raw_state)
        claimed = await asyncio.to_thread(self._claim, self._path(digest))
        if claimed is None:
            raise RemoteMCPOAuthStateError("oauth_state_missing_or_used")
        owner = ""
        secret_ref = ""
        try:
            pointer = await read_json_or_none(claimed)
            owner, secret_ref, expires_at = self._validate_pointer(pointer, digest)
            moment = int(now if now is not None else time.time())
            if expires_at < moment:
                raise RemoteMCPOAuthStateError("oauth_state_expired")
            raw_secret = await self._secret_store.get(
                owner_subject=owner, secret_ref=secret_ref
            )
            if not raw_secret:
                raise RemoteMCPOAuthStateError("oauth_state_secret_unavailable")
            try:
                payload = json.loads(raw_secret)
            except (TypeError, ValueError) as exc:
                raise RemoteMCPOAuthStateError("oauth_state_secret_invalid") from exc
            if not isinstance(payload, Mapping):
                raise RemoteMCPOAuthStateError("oauth_state_secret_invalid")
            if _clean(payload.get("schema")) != OAUTH_STATE_SECRET_SCHEMA:
                raise RemoteMCPOAuthStateError("oauth_state_secret_schema_mismatch")
            if not secrets.compare_digest(
                _clean(payload.get("state_digest")), digest
            ):
                raise RemoteMCPOAuthStateError("oauth_state_secret_mismatch")
            transaction = payload.get("transaction")
            if not isinstance(transaction, Mapping):
                raise RemoteMCPOAuthStateError("oauth_transaction_invalid")
            return dict(transaction)
        finally:
            await asyncio.to_thread(claimed.unlink, missing_ok=True)
            if owner and secret_ref:
                await self._secret_store.delete(
                    owner_subject=owner, secret_ref=secret_ref
                )

    async def purge_expired(self, *, now: int | None = None) -> int:
        moment = int(now if now is not None else time.time())
        removed = 0
        for name in await list_child_names(self._root):
            if len(name) != 69 or not name.endswith(".json"):
                continue
            source = self._root / name
            pointer = await read_json_or_none(source)
            if not isinstance(pointer, Mapping):
                continue
            try:
                expires_at = int(pointer.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at >= moment:
                continue
            claimed = await asyncio.to_thread(self._claim, source)
            if claimed is None:
                continue
            try:
                claimed_pointer = await read_json_or_none(claimed)
                owner = _clean(
                    claimed_pointer.get("owner_subject")
                    if isinstance(claimed_pointer, Mapping)
                    else ""
                )
                secret_ref = _clean(
                    claimed_pointer.get("secret_ref")
                    if isinstance(claimed_pointer, Mapping)
                    else ""
                )
                if owner and secret_ref:
                    await self._secret_store.delete(
                        owner_subject=owner, secret_ref=secret_ref
                    )
                removed += 1
            finally:
                await asyncio.to_thread(claimed.unlink, missing_ok=True)
        return removed

    @staticmethod
    def _claim(source: pathlib.Path) -> pathlib.Path | None:
        claimed = source.with_name(f".{source.name}.claimed.{uuid.uuid4().hex}")
        try:
            source.replace(claimed)
        except FileNotFoundError:
            return None
        return claimed

    @staticmethod
    def _validate_pointer(
        value: Any, expected_digest: str
    ) -> tuple[str, str, int]:
        if not isinstance(value, Mapping):
            raise RemoteMCPOAuthStateError("oauth_state_pointer_invalid")
        if _clean(value.get("schema")) != OAUTH_STATE_POINTER_SCHEMA:
            raise RemoteMCPOAuthStateError("oauth_state_pointer_schema_mismatch")
        digest = _clean(value.get("state_digest"))
        if not secrets.compare_digest(digest, expected_digest):
            raise RemoteMCPOAuthStateError("oauth_state_pointer_mismatch")
        owner = _clean(value.get("owner_subject"))
        secret_ref = _clean(value.get("secret_ref"))
        if not owner or not secret_ref:
            raise RemoteMCPOAuthStateError("oauth_state_pointer_invalid")
        try:
            expires_at = int(value.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise RemoteMCPOAuthStateError("oauth_state_pointer_invalid") from exc
        return owner, secret_ref, expires_at


__all__ = [
    "OAUTH_STATE_DEFAULT_TTL_SECONDS",
    "OAUTH_STATE_DIRNAME",
    "OAUTH_STATE_LAYOUT_VERSION",
    "OAUTH_STATE_MAX_TTL_SECONDS",
    "OAUTH_STATE_POINTER_SCHEMA",
    "OAUTH_STATE_SECRET_SCHEMA",
    "BundleStorageRemoteMCPOAuthStateStore",
    "RemoteMCPOAuthStateError",
    "RemoteMCPOAuthStateHandle",
]

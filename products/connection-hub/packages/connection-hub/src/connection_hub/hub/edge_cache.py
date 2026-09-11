# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis serving projection for durable connection-edge principal mappings."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from connection_hub.delegated_credentials.cache_io import (
    decode_cache_value,
    encode_cache_value,
)
from connection_hub.hub.edges import edge_actor, edge_target

PLATFORM_PRINCIPAL_CACHE_SCHEMA = "connection_hub.platform_principal_cache.v1"
EDGE_MUTATION_LOCK_TTL_SECONDS = 30
EDGE_MUTATION_LOCK_WAIT_SECONDS = 10.0


class ConnectionEdgeRuntimeCacheError(RuntimeError):
    """Shared edge projection or coordination is unavailable."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ConnectionEdgeRuntimeCache:
    """Shared read-through projection of identity-to-principal edges.

    The durable edge remains authoritative. Redis only removes filesystem work
    from the authentication path and can be rebuilt after eviction or restart.
    """

    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._redis = redis
        self._tenant = _clean(tenant) or "default"
        self._project = _clean(project) or "default"

    @staticmethod
    def identity_digest(*, authority_id: str, subject: str) -> str:
        material = f"{_clean(authority_id)}\0{_clean(subject)}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def key(self, *, authority_id: str, subject: str) -> str:
        digest = self.identity_digest(authority_id=authority_id, subject=subject)
        return f"{self._tenant}:{self._project}:kdcube:connection-edge:principal:{digest}"

    def mutation_lock_key(self) -> str:
        return f"{self._tenant}:{self._project}:kdcube:connection-edge:mutation-lock"

    @asynccontextmanager
    async def mutation_lock(self) -> AsyncIterator[None]:
        """Serialize durable edge/challenge mutations across runtime workers."""

        key = self.mutation_lock_key()
        token = secrets.token_hex(24)
        deadline = time.monotonic() + EDGE_MUTATION_LOCK_WAIT_SECONDS
        while True:
            try:
                acquired = await self._redis.set(
                    key,
                    token,
                    nx=True,
                    ex=EDGE_MUTATION_LOCK_TTL_SECONDS,
                )
            except Exception as exc:
                raise ConnectionEdgeRuntimeCacheError(
                    "connection-edge coordination is unavailable"
                ) from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise ConnectionEdgeRuntimeCacheError(
                    "timed out waiting for connection-edge mutation lock"
                )
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            try:
                await self._redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    key,
                    token,
                )
            except Exception:
                # The lease expires. Never delete without comparing ownership:
                # another worker may have acquired the key after expiry.
                pass

    async def read(self, *, authority_id: str, subject: str) -> dict[str, Any] | None:
        authority = _clean(authority_id)
        source_subject = _clean(subject)
        try:
            payload = decode_cache_value(
                await self._redis.get(
                    self.key(authority_id=authority, subject=source_subject)
                )
            )
        except Exception as exc:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection is unavailable"
            ) from exc
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema") != PLATFORM_PRINCIPAL_CACHE_SCHEMA:
            return None
        if _clean(payload.get("authority_id")) != authority:
            return None
        if _clean(payload.get("subject")) != source_subject:
            return None
        if not _clean(payload.get("platform_user_id")):
            return None
        return payload

    async def publish_edge(self, edge: Mapping[str, Any]) -> dict[str, Any]:
        source = edge_actor(edge)
        target = edge_target(edge)
        payload = {
            "schema": PLATFORM_PRINCIPAL_CACHE_SCHEMA,
            "authority_id": _clean(source.get("authority_id")),
            "provider": _clean(source.get("provider")).lower(),
            "subject": _clean(source.get("subject")),
            "platform_user_id": _clean(target.get("user_id")),
            "edge_id": _clean(edge.get("edge_id")),
        }
        if not payload["authority_id"] or not payload["subject"] or not payload["platform_user_id"]:
            raise ValueError("connection edge lacks a source identity or platform target")
        try:
            await self._redis.set(
                self.key(authority_id=payload["authority_id"], subject=payload["subject"]),
                encode_cache_value(payload),
            )
        except Exception as exc:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection is unavailable"
            ) from exc
        return payload

    async def remove(self, *, authority_id: str, subject: str) -> bool:
        try:
            removed = await self._redis.delete(
                self.key(authority_id=authority_id, subject=subject)
            )
        except Exception as exc:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection is unavailable"
            ) from exc
        return bool(removed)


__all__ = [
    "ConnectionEdgeRuntimeCache",
    "ConnectionEdgeRuntimeCacheError",
    "PLATFORM_PRINCIPAL_CACHE_SCHEMA",
]

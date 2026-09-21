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
from connection_hub.hub.edges import edge_actor, edge_revision, edge_target

PLATFORM_PRINCIPAL_CACHE_SCHEMA = "connection_hub.platform_principal_cache.v2"
EDGE_MUTATION_LOCK_TTL_SECONDS = 30
EDGE_MUTATION_LOCK_WAIT_SECONDS = 10.0


_PUBLISH_EDGE_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local ok, decoded = pcall(cjson.decode, existing)
  if ok and type(decoded) == 'table'
      and decoded['redis_run_id'] == ARGV[3] then
    local existing_revision = tonumber(decoded['edge_revision'] or 0)
    local incoming_revision = tonumber(ARGV[2])
    if existing_revision > incoming_revision then return 0 end
    if existing_revision == incoming_revision then
      if existing == ARGV[1] then return 1 end
      return -1
    end
  end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""


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
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.info("server")
            pipeline.get(self.key(authority_id=authority, subject=source_subject))
            info, raw = await pipeline.execute()
            payload = decode_cache_value(raw)
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
        current_run_id = _clean((info or {}).get("run_id"))
        if not current_run_id or _clean(payload.get("redis_run_id")) != current_run_id:
            return None
        try:
            revision = int(payload.get("edge_revision") or 0)
        except (TypeError, ValueError):
            return None
        if revision < 1:
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
        try:
            info = await self._redis.info("server")
        except Exception as exc:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection is unavailable"
            ) from exc
        run_id = _clean((info or {}).get("run_id"))
        if not run_id:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge Redis run id is unavailable"
            )
        payload = {
            "schema": PLATFORM_PRINCIPAL_CACHE_SCHEMA,
            "redis_run_id": run_id,
            "edge_revision": edge_revision(edge),
            "authority_id": _clean(source.get("authority_id")),
            "provider": _clean(source.get("provider")).lower(),
            "subject": _clean(source.get("subject")),
            "platform_user_id": _clean(target.get("user_id")),
            "edge_id": _clean(edge.get("edge_id")),
        }
        if not payload["authority_id"] or not payload["subject"] or not payload["platform_user_id"]:
            raise ValueError("connection edge lacks a source identity or platform target")
        try:
            installed = await self._redis.eval(
                _PUBLISH_EDGE_LUA,
                1,
                self.key(authority_id=payload["authority_id"], subject=payload["subject"]),
                encode_cache_value(payload),
                str(payload["edge_revision"]),
                run_id,
            )
        except Exception as exc:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection is unavailable"
            ) from exc
        result = int(installed or 0)
        if result < 0:
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection conflicts at the same revision"
            )
        if result == 0:
            current = await self.read(
                authority_id=payload["authority_id"],
                subject=payload["subject"],
            )
            if current is not None and int(current.get("edge_revision") or 0) > int(
                payload["edge_revision"]
            ):
                return current
            raise ConnectionEdgeRuntimeCacheError(
                "connection-edge projection rejected an older revision without a readable successor"
            )
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

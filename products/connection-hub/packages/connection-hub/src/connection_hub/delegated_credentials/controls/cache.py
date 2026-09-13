# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis projection for project control cards.

The project is the durable owner. Redis is the live authorization projection.
An updating marker makes every bound Card fail closed while the project changes
its ceiling; a missing or malformed projection is never treated as no ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connection_hub.delegated_credentials.cache_io import (
    decode_cache_value,
    encode_cache_value,
)
from connection_hub.delegated_credentials.controls.model import (
    ProjectControlCardAuthority,
    ProjectControlCardError,
)

CONTROL_CACHE_KIND_CARD = "control_card"
CONTROL_CACHE_KIND_UPDATING = "updating"
CONTROL_CACHE_KIND_RETIRED = "retired"

_CLAIM_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local ok, decoded = pcall(cjson.decode, existing)
  if not ok or type(decoded) ~= 'table' then return 0 end
  if decoded['kind'] ~= 'control_card' then return 0 end
  if tonumber(decoded['revision']) ~= tonumber(ARGV[2]) then return 0 end
elseif tonumber(ARGV[2]) ~= 0 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
"""

_FINALIZE_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local ok, decoded = pcall(cjson.decode, existing)
  if not ok or type(decoded) ~= 'table' then return 0 end
  local owned = decoded['kind'] == 'updating' and decoded['mutation_id'] == ARGV[2]
  if not owned then
    local incoming = tonumber(ARGV[3]) or 0
    local live = tonumber(decoded['revision']) or 0
    if decoded['kind'] ~= 'control_card' or live >= incoming then return 0 end
  end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""

_RESTORE_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
  local ok, decoded = pcall(cjson.decode, existing)
  if not ok or type(decoded) ~= 'table' then return 0 end
  if decoded['kind'] ~= 'control_card' then return 0 end
  if tonumber(decoded['revision']) >= tonumber(ARGV[2]) then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""

_REMOVE_MARKER_LUA = """
local existing = redis.call('GET', KEYS[1])
if not existing then return 1 end
local ok, decoded = pcall(cjson.decode, existing)
if not ok or type(decoded) ~= 'table' then return 0 end
if decoded['kind'] ~= 'updating' or decoded['mutation_id'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""

_ROLLBACK_LUA = """
local existing = redis.call('GET', KEYS[1])
if not existing then return 0 end
local ok, decoded = pcall(cjson.decode, existing)
if not ok or type(decoded) ~= 'table' then return 0 end
if decoded['kind'] ~= 'updating' or decoded['mutation_id'] ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""


class ControlCardCacheUnusable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ControlCardCacheEntry:
    kind: str
    authority: ProjectControlCardAuthority | None = None
    revision: int = 0
    mutation_id: str = ""

    @property
    def is_card(self) -> bool:
        return self.kind == CONTROL_CACHE_KIND_CARD

    @property
    def is_updating(self) -> bool:
        return self.kind == CONTROL_CACHE_KIND_UPDATING

    @property
    def is_retired(self) -> bool:
        return self.kind == CONTROL_CACHE_KIND_RETIRED


class ControlCardRuntimeCache:
    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._redis = redis
        self._tenant = str(tenant or "").strip()
        self._project = str(project or "").strip()

    def key(self, control_id: str) -> str:
        value = str(control_id or "").strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("control_card_id_invalid")
        return (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            f"control-card:{value}"
        )

    async def read(self, control_id: str) -> ControlCardCacheEntry | None:
        raw = await self._redis.get(self.key(control_id))
        if raw is None:
            return None
        payload = decode_cache_value(raw)
        if payload is None:
            raise ControlCardCacheUnusable("cached_control_card_not_decodable")
        kind = str(payload.get("kind") or "").strip()
        revision = int(payload.get("revision") or 0)
        if kind == CONTROL_CACHE_KIND_UPDATING:
            return ControlCardCacheEntry(
                kind=kind,
                revision=revision,
                mutation_id=str(payload.get("mutation_id") or ""),
            )
        if kind == CONTROL_CACHE_KIND_RETIRED:
            return ControlCardCacheEntry(kind=kind, revision=revision)
        if kind != CONTROL_CACHE_KIND_CARD:
            raise ControlCardCacheUnusable("cached_control_card_kind_unknown")
        try:
            authority = ProjectControlCardAuthority.from_mapping(
                payload.get("authority")
            )
        except ProjectControlCardError as exc:
            raise ControlCardCacheUnusable("cached_control_card_invalid") from exc
        return ControlCardCacheEntry(
            kind=kind,
            authority=authority,
            revision=authority.revision,
        )

    async def claim_transition(
        self,
        control_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        ttl_seconds: int = 30,
    ) -> bool:
        marker = {
            "kind": CONTROL_CACHE_KIND_UPDATING,
            "mutation_id": str(mutation_id),
            "revision": int(expected_revision),
        }
        return await self._eval_bool(
            _CLAIM_LUA,
            control_id,
            encode_cache_value(marker),
            str(int(expected_revision)),
            str(max(1, int(ttl_seconds))),
        )

    async def commit_projection(
        self,
        authority: ProjectControlCardAuthority,
        *,
        mutation_id: str,
    ) -> bool:
        payload = {
            "kind": CONTROL_CACHE_KIND_CARD,
            "revision": authority.revision,
            "authority": authority.to_dict(),
        }
        return await self._eval_bool(
            _FINALIZE_LUA,
            authority.control_id,
            encode_cache_value(payload),
            str(mutation_id),
            str(authority.revision),
        )

    async def restore_projection(
        self,
        authority: ProjectControlCardAuthority,
    ) -> bool:
        payload = {
            "kind": CONTROL_CACHE_KIND_CARD,
            "revision": authority.revision,
            "authority": authority.to_dict(),
        }
        return await self._eval_bool(
            _RESTORE_LUA,
            authority.control_id,
            encode_cache_value(payload),
            str(authority.revision),
        )

    async def rollback_transition(
        self,
        authority: ProjectControlCardAuthority,
        *,
        mutation_id: str,
    ) -> bool:
        """Restore the previous projection only while this mutation owns it."""

        payload = {
            "kind": CONTROL_CACHE_KIND_CARD,
            "revision": authority.revision,
            "authority": authority.to_dict(),
        }
        return await self._eval_bool(
            _ROLLBACK_LUA,
            authority.control_id,
            encode_cache_value(payload),
            str(mutation_id),
        )

    async def abandon_transition(self, control_id: str, *, mutation_id: str) -> bool:
        return await self._eval_bool(
            _REMOVE_MARKER_LUA,
            control_id,
            str(mutation_id),
        )

    async def _eval_bool(self, script: str, control_id: str, *args: str) -> bool:
        result = await self._redis.eval(script, 1, self.key(control_id), *args)
        return bool(int(result or 0))


__all__ = [
    "CONTROL_CACHE_KIND_CARD",
    "CONTROL_CACHE_KIND_RETIRED",
    "CONTROL_CACHE_KIND_UPDATING",
    "ControlCardCacheEntry",
    "ControlCardCacheUnusable",
    "ControlCardRuntimeCache",
]

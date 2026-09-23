# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Read-only Redis scanning primitives for authority migrations."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_READ_VALUE_AND_EXPIRY = """
local value = redis.call('GET', KEYS[1])
if not value then
  return {false, -2}
end
local absolute = redis.pcall('PEXPIRETIME', KEYS[1])
if type(absolute) == 'number' then
  return {value, absolute}
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
  return {value, ttl}
end
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
return {value, now_ms + ttl}
"""


class DurableRedisRecordError(RuntimeError):
    """A durable source record is malformed, unstable, or incomplete."""

    def __init__(self, reason: str, *, key: str = "") -> None:
        self.reason = str(reason or "durable_redis_record_invalid")
        source_key = str(key or "")
        self.key_sha256 = (
            hashlib.sha256(source_key.encode("utf-8")).hexdigest()
            if source_key
            else ""
        )
        suffix = f": key_sha256={self.key_sha256}" if self.key_sha256 else ""
        super().__init__(self.reason + suffix)


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def decode_redis_json_object(raw: Any, *, key: str) -> dict[str, Any]:
    """Decode one Redis value as a JSON object with a key-safe error."""

    try:
        parsed = json.loads(_text(raw))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise DurableRedisRecordError(
            "durable_redis_record_json_invalid",
            key=key,
        ) from exc
    if not isinstance(parsed, dict):
        raise DurableRedisRecordError(
            "durable_redis_record_not_an_object",
            key=key,
        )
    return dict(parsed)


class ReadOnlyRedisMigrationScanner:
    """Scan keys and atomically read values with their absolute expiry."""

    def __init__(self, redis: Any, *, scan_count: int = 200) -> None:
        if redis is None:
            raise ValueError("ReadOnlyRedisMigrationScanner requires redis")
        self._redis = redis
        self._scan_count = max(1, min(int(scan_count), 10_000))

    async def keys(self, pattern: str) -> list[str]:
        cursor: int | str | bytes = 0
        found: set[str] = set()
        while True:
            cursor, values = await self._redis.scan(
                cursor=cursor,
                match=pattern,
                count=self._scan_count,
            )
            for value in values or ():
                found.add(_text(value))
            if int(cursor) == 0:
                return sorted(found)

    async def read(
        self,
        key: str,
        *,
        expiry_required: bool = True,
    ) -> tuple[Any, int | None]:
        result = await self._redis.eval(_READ_VALUE_AND_EXPIRY, 1, key)
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise DurableRedisRecordError(
                "durable_redis_atomic_read_invalid",
                key=key,
            )
        raw, raw_expiry = result
        if raw in (None, False) or int(raw_expiry) == -2:
            raise DurableRedisRecordError(
                "durable_redis_record_disappeared_during_scan",
                key=key,
            )
        expiry = int(raw_expiry)
        if expiry == -1:
            if expiry_required:
                raise DurableRedisRecordError(
                    "durable_redis_record_expiry_missing",
                    key=key,
                )
            return raw, None
        if expiry <= 0:
            raise DurableRedisRecordError(
                "durable_redis_record_expiry_invalid",
                key=key,
            )
        return raw, expiry

    async def read_json_object(
        self,
        key: str,
        *,
        expiry_required: bool = True,
    ) -> tuple[dict[str, Any], int | None]:
        raw, expiry = await self.read(key, expiry_required=expiry_required)
        return decode_redis_json_object(raw, key=key), expiry

    async def read_text(
        self,
        key: str,
        *,
        expiry_required: bool = True,
    ) -> tuple[str, int | None]:
        raw, expiry = await self.read(key, expiry_required=expiry_required)
        try:
            return _text(raw), expiry
        except UnicodeDecodeError as exc:
            raise DurableRedisRecordError(
                "durable_redis_record_text_invalid",
                key=key,
            ) from exc


__all__ = [
    "DurableRedisRecordError",
    "ReadOnlyRedisMigrationScanner",
    "decode_redis_json_object",
]

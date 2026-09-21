# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Secret-backed, single-use state with a Redis restart fence.

The callback state and its payload live in a host-provided secret store. Redis
contains only a digest-keyed pointer. A pointer records the Redis ``run_id``
that created it, and claim reads that run id, the pointer, and deletes the
pointer in one transaction. Restoring an older Redis snapshot therefore
invalidates the restored pointer instead of reviving a consumed operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


ONE_TIME_POINTER_SCHEMA = "connection_hub.one_time_state.pointer.v1"
ONE_TIME_SECRET_SCHEMA = "connection_hub.one_time_state.secret.v1"
ONE_TIME_STATE_MAX_TTL_SECONDS = 3600

_PURPOSE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_LOGGER = logging.getLogger(__name__)


class OneTimeStateError(ValueError):
    """A state record cannot be created or safely consumed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OneTimeStateSecretStore(Protocol):
    """Host-owned custody for one short-lived secret record."""

    async def set(
        self,
        *,
        secret_ref: str,
        value: str,
        expires_at: int,
    ) -> None: ...

    async def get(self, *, secret_ref: str) -> str | None: ...

    async def delete(self, *, secret_ref: str) -> None: ...

    async def purge_expired(self, *, now: int, limit: int) -> int: ...


@dataclass(frozen=True)
class ClaimedOneTimePointer:
    record: Mapping[str, Any]
    current_run_id: str
    belongs_to_current_run: bool


def state_digest(state: str) -> str:
    """Return the non-secret lookup identity for a callback state."""

    return hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return str(value or "")


def _json_mapping(value: Any, *, reason: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise OneTimeStateError(reason) from exc
    if not isinstance(parsed, Mapping):
        raise OneTimeStateError(reason)
    return dict(parsed)


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=True,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise OneTimeStateError("one_time_state_payload_not_json") from exc
    if not isinstance(copied, dict):
        raise OneTimeStateError("one_time_state_payload_invalid")
    return copied


class RedisRunBoundPointerStore:
    """Digest-keyed Redis pointers that are valid for one Redis run only."""

    def __init__(self, redis: Any, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = str(prefix or "connection-hub:one-time-state").strip(":")

    def key(self, digest: str) -> str:
        normalized = str(digest or "").strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise OneTimeStateError("one_time_state_digest_invalid")
        return f"{self._prefix}:{normalized}"

    async def _run_id(self) -> str:
        info = await self._redis.info("server")
        run_id = str((info or {}).get("run_id") or "").strip()
        if not run_id:
            raise OneTimeStateError("one_time_state_redis_run_id_unavailable")
        return run_id

    async def put(
        self,
        *,
        digest: str,
        record: Mapping[str, Any],
        ttl_seconds: int,
    ) -> None:
        run_id = await self._run_id()
        value = {**dict(record), "redis_run_id": run_id}
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._redis.set(self.key(digest), encoded, ex=int(ttl_seconds))

    async def claim(self, *, digest: str) -> ClaimedOneTimePointer | None:
        """Atomically read the live run, read the pointer, and consume it."""

        pipeline = self._redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.get(self.key(digest))
        pipeline.delete(self.key(digest))
        info, raw, _deleted = await pipeline.execute()
        if raw is None:
            return None
        run_id = str((info or {}).get("run_id") or "").strip()
        if not run_id:
            raise OneTimeStateError("one_time_state_redis_run_id_unavailable")
        record = _json_mapping(raw, reason="one_time_state_pointer_invalid")
        recorded_run_id = str(record.get("redis_run_id") or "").strip()
        return ClaimedOneTimePointer(
            record=record,
            current_run_id=run_id,
            belongs_to_current_run=bool(recorded_run_id) and hmac.compare_digest(
                recorded_run_id,
                run_id,
            ),
        )


class SecretBackedOneTimeStateStore:
    """One-use state whose only recoverable payload is in a secret store."""

    def __init__(
        self,
        *,
        pointers: RedisRunBoundPointerStore,
        secrets: OneTimeStateSecretStore,
        purpose: str,
        max_ttl_seconds: int = ONE_TIME_STATE_MAX_TTL_SECONDS,
    ) -> None:
        normalized_purpose = str(purpose or "").strip().lower()
        if not _PURPOSE_PATTERN.fullmatch(normalized_purpose):
            raise OneTimeStateError("one_time_state_purpose_invalid")
        self._pointers = pointers
        self._secrets = secrets
        self._purpose = normalized_purpose
        self._max_ttl_seconds = int(max_ttl_seconds)

    async def put(
        self,
        state: str,
        payload: Mapping[str, Any],
        *,
        ttl_seconds: int,
        now: int | None = None,
    ) -> None:
        raw_state = str(state or "").strip()
        if not raw_state:
            raise OneTimeStateError("one_time_state_missing")
        ttl = int(ttl_seconds)
        if ttl < 1 or ttl > self._max_ttl_seconds:
            raise OneTimeStateError("one_time_state_ttl_invalid")
        moment = int(now if now is not None else time.time())
        expires_at = moment + ttl
        digest = state_digest(raw_state)
        secret_ref = uuid.uuid4().hex
        secret_record = {
            "schema": ONE_TIME_SECRET_SCHEMA,
            "purpose": self._purpose,
            "state": raw_state,
            "state_digest": digest,
            "payload": _json_copy(payload),
            "created_at": moment,
            "expires_at": expires_at,
        }
        pointer_record = {
            "schema": ONE_TIME_POINTER_SCHEMA,
            "purpose": self._purpose,
            "state_digest": digest,
            "secret_ref": secret_ref,
            "created_at": moment,
            "expires_at": expires_at,
        }
        await self._purge_expired_best_effort(now=moment)
        await self._secrets.set(
            secret_ref=secret_ref,
            value=json.dumps(
                secret_record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            expires_at=expires_at,
        )
        try:
            await self._pointers.put(
                digest=digest,
                record=pointer_record,
                ttl_seconds=ttl,
            )
        except Exception:
            try:
                await self._secrets.delete(secret_ref=secret_ref)
            except Exception:
                _LOGGER.warning(
                    "[connection-hub.one-time-state] compensating secret cleanup failed",
                    exc_info=True,
                )
            raise

    async def pop(
        self,
        state: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        raw_state = str(state or "").strip()
        if not raw_state:
            return None
        digest = state_digest(raw_state)
        claimed = await self._pointers.claim(digest=digest)
        if claimed is None:
            return None
        try:
            pointer = self._validate_pointer(claimed.record, digest=digest)
        except OneTimeStateError as exc:
            # Deployments that replace the former payload-in-Redis format can
            # still receive a callback issued by the previous process. The
            # pointer was consumed atomically; make that callback restart the
            # flow instead of turning an expected cutover miss into a 500.
            _LOGGER.warning(
                "[connection-hub.one-time-state] consumed invalid pointer reason=%s",
                exc.reason,
            )
            return None
        secret_ref = str(pointer["secret_ref"])
        try:
            moment = int(now if now is not None else time.time())
            if not claimed.belongs_to_current_run or int(pointer["expires_at"]) <= moment:
                return None
            raw_secret = await self._secrets.get(secret_ref=secret_ref)
            if raw_secret is None:
                return None
            secret = self._validate_secret(
                _json_mapping(raw_secret, reason="one_time_state_secret_invalid"),
                state=raw_state,
                digest=digest,
            )
            if int(secret["expires_at"]) <= moment:
                return None
            return dict(secret["payload"])
        finally:
            try:
                await self._secrets.delete(secret_ref=secret_ref)
            except Exception:
                # The pointer is already consumed, so cleanup cannot alter the
                # callback result. Expiry metadata lets the bounded purge remove
                # the orphan later.
                _LOGGER.warning(
                    "[connection-hub.one-time-state] consumed secret cleanup failed",
                    exc_info=True,
                )

    async def _purge_expired_best_effort(self, *, now: int) -> None:
        try:
            await self._secrets.purge_expired(now=now, limit=100)
        except Exception:
            _LOGGER.warning(
                "[connection-hub.one-time-state] expired secret cleanup failed",
                exc_info=True,
            )

    def _validate_pointer(
        self,
        value: Mapping[str, Any],
        *,
        digest: str,
    ) -> dict[str, Any]:
        record = dict(value)
        if str(record.get("schema") or "") != ONE_TIME_POINTER_SCHEMA:
            raise OneTimeStateError("one_time_state_pointer_schema_mismatch")
        if str(record.get("purpose") or "") != self._purpose:
            raise OneTimeStateError("one_time_state_pointer_purpose_mismatch")
        if not hmac.compare_digest(str(record.get("state_digest") or ""), digest):
            raise OneTimeStateError("one_time_state_pointer_digest_mismatch")
        secret_ref = str(record.get("secret_ref") or "").strip()
        if not secret_ref:
            raise OneTimeStateError("one_time_state_pointer_invalid")
        try:
            record["expires_at"] = int(record.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise OneTimeStateError("one_time_state_pointer_invalid") from exc
        record["secret_ref"] = secret_ref
        return record

    def _validate_secret(
        self,
        value: Mapping[str, Any],
        *,
        state: str,
        digest: str,
    ) -> dict[str, Any]:
        record = dict(value)
        if str(record.get("schema") or "") != ONE_TIME_SECRET_SCHEMA:
            raise OneTimeStateError("one_time_state_secret_schema_mismatch")
        if str(record.get("purpose") or "") != self._purpose:
            raise OneTimeStateError("one_time_state_secret_purpose_mismatch")
        if not hmac.compare_digest(str(record.get("state_digest") or ""), digest):
            raise OneTimeStateError("one_time_state_secret_digest_mismatch")
        if not hmac.compare_digest(str(record.get("state") or ""), state):
            raise OneTimeStateError("one_time_state_secret_state_mismatch")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise OneTimeStateError("one_time_state_payload_invalid")
        try:
            record["expires_at"] = int(record.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise OneTimeStateError("one_time_state_secret_invalid") from exc
        record["payload"] = dict(payload)
        return record


__all__ = [
    "ONE_TIME_POINTER_SCHEMA",
    "ONE_TIME_SECRET_SCHEMA",
    "ONE_TIME_STATE_MAX_TTL_SECONDS",
    "ClaimedOneTimePointer",
    "OneTimeStateError",
    "OneTimeStateSecretStore",
    "RedisRunBoundPointerStore",
    "SecretBackedOneTimeStateStore",
    "state_digest",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis adapter for short-lived OAuth device authorization requests.

Only digests are used as lookup keys. Device records contain consent context
and the approved Card projection, never an issued access or refresh token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.oauth.device import (
    DEVICE_POLL_INTERVAL_SECONDS,
    DEVICE_REQUEST_TTL_SECONDS,
    DEVICE_SLOW_DOWN_SECONDS,
    DEVICE_USER_CODE_ATTEMPTS,
    DEVICE_USER_CODE_WINDOW_SECONDS,
    DEVICE_TERMINAL_ERRORS,
    DeviceAuthorizationIssue,
    DevicePollResult,
    DeviceUserCodeResult,
    assert_device_record_safe,
    decode_device_record,
    device_code_digest,
    encode_device_record,
    generate_user_code,
    new_device_record,
    user_code_digest,
)
from connection_hub.delegated_credentials.oauth.store import GrantStoreUnavailable


_CREATE_DEVICE_REQUEST = """
-- connection_hub_device_create_v1
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
redis.call('SETEX', KEYS[1], ARGV[1], ARGV[2])
redis.call('SETEX', KEYS[2], ARGV[1], ARGV[3])
return 1
"""

_READ_DEVICE_USER_CODE = """
-- connection_hub_device_user_code_read_v1
local attempts = tonumber(redis.call('GET', KEYS[2]) or '0')
local maximum = tonumber(ARGV[1])
if attempts >= maximum then
    return cjson.encode({status='rate_limited'})
end
local pointer = redis.call('GET', KEYS[1])
if pointer then
    return cjson.encode({status='found', device_digest=pointer})
end
attempts = redis.call('INCR', KEYS[2])
if attempts == 1 then
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
end
return cjson.encode({status='not_found'})
"""

_DECIDE_DEVICE_REQUEST = """
-- connection_hub_device_decide_v1
local raw = redis.call('GET', KEYS[1])
if not raw then
    return 0
end
local pointer = redis.call('GET', KEYS[2])
if not pointer or tostring(pointer) ~= ARGV[5] then
    return 0
end
local record = cjson.decode(raw)
local now = tonumber(ARGV[1])
if tonumber(record['expires_at'] or 0) <= now then
    redis.call('DEL', KEYS[1], KEYS[2])
    return -1
end
if record['state'] ~= 'pending' then
    return -2
end
local ttl = redis.call('TTL', KEYS[1])
if ttl <= 0 then
    redis.call('DEL', KEYS[1], KEYS[2])
    return -1
end
record['state'] = ARGV[2]
record['approving_subject'] = ARGV[3]
if ARGV[2] == 'approved' then
    record['grant'] = cjson.decode(ARGV[4])
else
    record['grant'] = cjson.null
    record['terminal_error'] = ARGV[6]
end
redis.call('SETEX', KEYS[1], ttl, cjson.encode(record))
redis.call('DEL', KEYS[2])
return 1
"""

_POLL_DEVICE_REQUEST = """
-- connection_hub_device_poll_v1
local raw = redis.call('GET', KEYS[1])
if not raw then
    return cjson.encode({status='expired_token', interval=tonumber(ARGV[3])})
end
local record = cjson.decode(raw)
local now = tonumber(ARGV[1])
local client_id = ARGV[2]
local slow_down = tonumber(ARGV[4])
local interval = tonumber(record['interval'] or ARGV[3])
if tonumber(record['expires_at'] or 0) <= now then
    redis.call('DEL', KEYS[1])
    return cjson.encode({status='expired_token', interval=interval})
end
local ttl = redis.call('TTL', KEYS[1])
if ttl <= 0 then
    redis.call('DEL', KEYS[1])
    return cjson.encode({status='expired_token', interval=interval})
end
if tostring(record['client_id'] or '') ~= client_id then
    return cjson.encode({status='device_client_mismatch', interval=interval})
end
if record['state'] == 'pending' then
    if now < tonumber(record['next_poll_at'] or 0) then
        interval = interval + slow_down
        record['interval'] = interval
        record['next_poll_at'] = now + interval
        redis.call('SETEX', KEYS[1], ttl, cjson.encode(record))
        return cjson.encode({status='slow_down', interval=interval})
    end
    record['next_poll_at'] = now + interval
    redis.call('SETEX', KEYS[1], ttl, cjson.encode(record))
    return cjson.encode({status='authorization_pending', interval=interval})
end
if record['state'] == 'denied' then
    record['state'] = 'consumed'
    redis.call('SETEX', KEYS[1], ttl, cjson.encode(record))
    return cjson.encode({status=tostring(record['terminal_error'] or 'access_denied'), interval=interval})
end
if record['state'] == 'approved' then
    local authorization = record['grant']
    record['state'] = 'consumed'
    record['grant'] = cjson.null
    redis.call('SETEX', KEYS[1], ttl, cjson.encode(record))
    return cjson.encode({status='approved', interval=interval, authorization=authorization})
end
return cjson.encode({status='device_code_replayed', interval=interval})
"""


class DeviceGrantStore:
    """Atomic state machine for one RFC 8628 request."""

    def __init__(
        self,
        redis: Any,
        tenant: str,
        project: str,
        *,
        request_ttl: int = DEVICE_REQUEST_TTL_SECONDS,
        poll_interval: int = DEVICE_POLL_INTERVAL_SECONDS,
        max_user_code_attempts: int = DEVICE_USER_CODE_ATTEMPTS,
        user_code_window: int = DEVICE_USER_CODE_WINDOW_SECONDS,
    ) -> None:
        self._redis = redis
        self._tenant = str(tenant or "").strip()
        self._project = str(project or "").strip()
        self._request_ttl = max(1, int(request_ttl))
        self._poll_interval = max(1, int(poll_interval))
        self._max_user_code_attempts = max(1, int(max_user_code_attempts))
        self._user_code_window = max(1, int(user_code_window))

    async def _redis_call(
        self,
        operation: str,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            method = getattr(self._redis, method_name)
            return await method(*args, **kwargs)
        except GrantStoreUnavailable:
            raise
        except Exception as exc:
            raise GrantStoreUnavailable(operation) from exc

    def _key(self, kind: str, digest: str) -> str:
        return (
            f"{self._tenant}:{self._project}:kdcube:oauth:"
            f"device:{kind}:{digest}"
        )

    @staticmethod
    def _attempt_digest(attempt_key: str) -> str:
        candidate = str(attempt_key or "").strip()
        if not candidate:
            candidate = "anonymous"
        return hashlib.sha256(candidate.encode("utf-8")).hexdigest()

    async def create(
        self,
        *,
        client_id: str,
        scopes: list[str] | tuple[str, ...],
        resource: str = "",
        client_metadata: Mapping[str, Any] | None = None,
        requested_access_id: str = "",
        expected_card_revision: int | None = None,
        context: Mapping[str, Any] | None = None,
        now: int | None = None,
    ) -> DeviceAuthorizationIssue:
        created_at = int(time.time() if now is None else now)
        for _attempt in range(8):
            device_code = secrets.token_urlsafe(40)
            user_code = generate_user_code()
            device_digest = device_code_digest(device_code)
            user_digest = user_code_digest(user_code)
            record = new_device_record(
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                client_metadata=client_metadata,
                requested_access_id=requested_access_id,
                expected_card_revision=expected_card_revision,
                context=context,
                device_digest=device_digest,
                user_digest=user_digest,
                now=created_at,
                expires_in=self._request_ttl,
                interval=self._poll_interval,
            )
            created = await self._redis_call(
                "device_authorization.create",
                "eval",
                _CREATE_DEVICE_REQUEST,
                2,
                self._key("request", device_digest),
                self._key("user", user_digest),
                self._request_ttl,
                encode_device_record(record),
                device_digest,
            )
            if int(created) == 1:
                return DeviceAuthorizationIssue(
                    device_code=device_code,
                    user_code=user_code,
                    expires_in=self._request_ttl,
                    interval=self._poll_interval,
                )
        raise GrantStoreUnavailable("device_authorization.create")

    async def read_user_code(
        self,
        user_code: str,
        *,
        attempt_key: str,
        now: int | None = None,
    ) -> DeviceUserCodeResult:
        observed_at = int(time.time() if now is None else now)
        try:
            digest = user_code_digest(user_code)
        except ValueError:
            digest = ""
        raw_lookup = await self._redis_call(
            "device_authorization.user_code.read",
            "eval",
            _READ_DEVICE_USER_CODE,
            2,
            self._key("user", digest),
            self._key("guess", self._attempt_digest(attempt_key)),
            self._max_user_code_attempts,
            self._user_code_window,
        )
        try:
            lookup = json.loads(raw_lookup)
        except Exception as exc:
            raise GrantStoreUnavailable(
                "device_authorization.user_code.read"
            ) from exc
        if not isinstance(lookup, Mapping):
            raise GrantStoreUnavailable("device_authorization.user_code.read")
        status = str(lookup.get("status") or "not_found")
        if status != "found":
            return DeviceUserCodeResult(status=status)
        device_digest = str(lookup.get("device_digest") or "").strip()
        raw = await self._redis_call(
            "device_authorization.read",
            "get",
            self._key("request", device_digest),
        )
        request = decode_device_record(raw)
        if (
            request is None
            or request.get("state") != "pending"
            or int(request.get("expires_at") or 0) <= observed_at
        ):
            await self._redis_call(
                "device_authorization.user_code.expire",
                "delete",
                self._key("user", digest),
                self._key("request", device_digest),
            )
            return DeviceUserCodeResult(status="not_found")
        return DeviceUserCodeResult(
            status="found",
            device_digest=device_digest,
            user_digest=digest,
            request=request,
        )

    async def approve(
        self,
        *,
        device_digest: str,
        user_digest: str,
        approving_subject: str,
        authorization: Mapping[str, Any],
        now: int | None = None,
    ) -> str:
        snapshot = dict(authorization or {})
        assert_device_record_safe(snapshot)
        return await self._decide(
            device_digest=device_digest,
            user_digest=user_digest,
            state="approved",
            approving_subject=approving_subject,
            authorization=snapshot,
            now=now,
        )

    async def deny(
        self,
        *,
        device_digest: str,
        user_digest: str,
        approving_subject: str,
        error: str = "access_denied",
        now: int | None = None,
    ) -> str:
        terminal_error = str(error or "").strip()
        if terminal_error not in DEVICE_TERMINAL_ERRORS:
            raise ValueError("device_terminal_error_invalid")
        return await self._decide(
            device_digest=device_digest,
            user_digest=user_digest,
            state="denied",
            approving_subject=approving_subject,
            authorization={},
            terminal_error=terminal_error,
            now=now,
        )

    async def _decide(
        self,
        *,
        device_digest: str,
        user_digest: str,
        state: str,
        approving_subject: str,
        authorization: Mapping[str, Any],
        now: int | None,
        terminal_error: str = "access_denied",
    ) -> str:
        digest = str(user_digest or "").strip()
        if len(digest) != 64:
            return "not_found"
        result = int(
            await self._redis_call(
                "device_authorization.decide",
                "eval",
                _DECIDE_DEVICE_REQUEST,
                2,
                self._key("request", str(device_digest or "").strip()),
                self._key("user", digest),
                int(time.time() if now is None else now),
                state,
                str(approving_subject or "").strip(),
                json.dumps(
                    dict(authorization or {}),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                str(device_digest or "").strip(),
                str(terminal_error or "access_denied"),
            )
        )
        return {
            1: state,
            0: "not_found",
            -1: "expired",
            -2: "already_decided",
        }.get(result, "unavailable")

    async def poll(
        self,
        *,
        device_code: str,
        client_id: str,
        now: int | None = None,
    ) -> DevicePollResult:
        try:
            digest = device_code_digest(device_code)
        except ValueError:
            return DevicePollResult(status="expired_token", interval=self._poll_interval)
        raw = await self._redis_call(
            "device_authorization.poll",
            "eval",
            _POLL_DEVICE_REQUEST,
            1,
            self._key("request", digest),
            int(time.time() if now is None else now),
            str(client_id or "").strip(),
            self._poll_interval,
            DEVICE_SLOW_DOWN_SECONDS,
        )
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise GrantStoreUnavailable("device_authorization.poll") from exc
        if not isinstance(payload, Mapping):
            raise GrantStoreUnavailable("device_authorization.poll")
        authorization = payload.get("authorization")
        if authorization is not None and not isinstance(authorization, Mapping):
            raise GrantStoreUnavailable("device_authorization.poll")
        if isinstance(authorization, Mapping):
            assert_device_record_safe(authorization)
        return DevicePollResult(
            status=str(payload.get("status") or "expired_token"),
            interval=max(1, int(payload.get("interval") or self._poll_interval)),
            authorization=(
                dict(authorization) if isinstance(authorization, Mapping) else None
            ),
        )


__all__ = ["DeviceGrantStore"]

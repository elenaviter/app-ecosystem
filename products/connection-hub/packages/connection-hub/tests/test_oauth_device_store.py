# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from connection_hub.delegated_credentials.oauth.device import (
    DEVICE_POLL_ACCESS_DENIED,
    DEVICE_POLL_APPROVED,
    DEVICE_POLL_AUTHORIZATION_PENDING,
    DEVICE_POLL_CLIENT_MISMATCH,
    DEVICE_POLL_EXPIRED,
    DEVICE_POLL_REPLAYED,
    DEVICE_POLL_SLOW_DOWN,
    assert_device_record_safe,
    user_code_digest,
)
from connection_hub.delegated_credentials.oauth.device_store import DeviceGrantStore
from connection_hub.delegated_credentials.oauth.metadata import (
    authorization_server_metadata,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        return self.values.get(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
            self.values.pop(key, None)
            self.ttls.pop(key, None)
        return removed

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        if key not in self.values:
            return False
        self.ttls[key] = int(ttl)
        return True

    async def eval(self, script: str, numkeys: int, *values):
        keys = [str(value) for value in values[:numkeys]]
        args = list(values[numkeys:])
        async with self._lock:
            if "connection_hub_device_create_v1" in script:
                if any(key in self.values for key in keys):
                    return 0
                ttl = int(args[0])
                self.values[keys[0]] = str(args[1])
                self.values[keys[1]] = str(args[2])
                self.ttls[keys[0]] = ttl
                self.ttls[keys[1]] = ttl
                return 1
            if "connection_hub_device_user_code_read_v1" in script:
                attempts = int(self.values.get(keys[1], "0"))
                maximum = int(args[0])
                if attempts >= maximum:
                    return json.dumps({"status": "rate_limited"})
                pointer = self.values.get(keys[0])
                if pointer is not None:
                    return json.dumps(
                        {"status": "found", "device_digest": pointer}
                    )
                attempts += 1
                self.values[keys[1]] = str(attempts)
                if attempts == 1:
                    self.ttls[keys[1]] = int(args[1])
                return json.dumps({"status": "not_found"})
            if "connection_hub_device_decide_v1" in script:
                raw = self.values.get(keys[0])
                if raw is None:
                    return 0
                if self.values.get(keys[1]) != str(args[4]):
                    return 0
                record = json.loads(raw)
                now = int(args[0])
                if int(record["expires_at"]) <= now:
                    await self.delete(*keys)
                    return -1
                if record["state"] != "pending":
                    return -2
                record["state"] = str(args[1])
                record["approving_subject"] = str(args[2])
                record["grant"] = (
                    json.loads(args[3]) if record["state"] == "approved" else None
                )
                if record["state"] == "denied":
                    record["terminal_error"] = str(args[5])
                self.values[keys[0]] = json.dumps(record)
                await self.delete(keys[1])
                return 1
            if "connection_hub_device_poll_v1" in script:
                raw = self.values.get(keys[0])
                default_interval = int(args[2])
                if raw is None:
                    return json.dumps(
                        {"status": DEVICE_POLL_EXPIRED, "interval": default_interval}
                    )
                record = json.loads(raw)
                now = int(args[0])
                client_id = str(args[1])
                interval = int(record.get("interval") or default_interval)
                if record["client_id"] != client_id:
                    return json.dumps(
                        {"status": DEVICE_POLL_CLIENT_MISMATCH, "interval": interval}
                    )
                if int(record["expires_at"]) <= now:
                    await self.delete(keys[0])
                    return json.dumps(
                        {"status": DEVICE_POLL_EXPIRED, "interval": interval}
                    )
                if record["state"] == "pending":
                    if now < int(record["next_poll_at"]):
                        interval += int(args[3])
                        record["interval"] = interval
                        record["next_poll_at"] = now + interval
                        self.values[keys[0]] = json.dumps(record)
                        return json.dumps(
                            {"status": DEVICE_POLL_SLOW_DOWN, "interval": interval}
                        )
                    record["next_poll_at"] = now + interval
                    self.values[keys[0]] = json.dumps(record)
                    return json.dumps(
                        {
                            "status": DEVICE_POLL_AUTHORIZATION_PENDING,
                            "interval": interval,
                        }
                    )
                if record["state"] == "denied":
                    record["state"] = "consumed"
                    self.values[keys[0]] = json.dumps(record)
                    return json.dumps(
                        {
                            "status": record.get(
                                "terminal_error", DEVICE_POLL_ACCESS_DENIED
                            ),
                            "interval": interval,
                        }
                    )
                if record["state"] == "approved":
                    authorization = record["grant"]
                    record["state"] = "consumed"
                    record["grant"] = None
                    self.values[keys[0]] = json.dumps(record)
                    return json.dumps(
                        {
                            "status": DEVICE_POLL_APPROVED,
                            "interval": interval,
                            "authorization": authorization,
                        }
                    )
                return json.dumps(
                    {"status": DEVICE_POLL_REPLAYED, "interval": interval}
                )
        raise AssertionError("unsupported script")


def _store(*, ttl: int = 60, attempts: int = 2) -> tuple[FakeRedis, DeviceGrantStore]:
    redis = FakeRedis()
    return redis, DeviceGrantStore(
        redis,
        "tenant",
        "project",
        request_ttl=ttl,
        poll_interval=5,
        max_user_code_attempts=attempts,
    )


@pytest.mark.asyncio
async def test_device_record_uses_digest_lookups_and_contains_no_codes_or_tokens():
    redis, store = _store()
    issue = await store.create(
        client_id="client-one",
        scopes=["work:read"],
        resource="https://example.test/mcp",
        now=100,
    )

    serialized = json.dumps(redis.values, sort_keys=True)
    assert issue.device_code not in serialized
    assert issue.user_code not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert all(issue.device_code not in key for key in redis.values)


@pytest.mark.asyncio
async def test_pending_poll_slow_down_and_client_mismatch_are_distinct():
    _, store = _store()
    issue = await store.create(client_id="client-one", scopes=[], now=100)

    wrong = await store.poll(
        device_code=issue.device_code,
        client_id="client-two",
        now=105,
    )
    assert wrong.status == DEVICE_POLL_CLIENT_MISMATCH

    pending = await store.poll(
        device_code=issue.device_code,
        client_id="client-one",
        now=105,
    )
    assert pending.status == DEVICE_POLL_AUTHORIZATION_PENDING
    early = await store.poll(
        device_code=issue.device_code,
        client_id="client-one",
        now=106,
    )
    assert early.status == DEVICE_POLL_SLOW_DOWN
    assert early.interval == 10


@pytest.mark.asyncio
async def test_approval_is_consumed_once_across_two_pollers():
    redis, store = _store()
    issue = await store.create(client_id="client-one", scopes=[], now=100)
    lookup = await store.read_user_code(
        issue.user_code,
        attempt_key="browser-session",
        now=101,
    )
    assert lookup.status == "found"
    assert await store.approve(
        device_digest=lookup.device_digest,
        user_digest=lookup.user_digest,
        approving_subject="user-1",
        authorization={"sub": "user-1", "registry_access_id": "aut_one"},
        now=101,
    ) == "approved"

    first, second = await asyncio.gather(
        store.poll(device_code=issue.device_code, client_id="client-one", now=105),
        store.poll(device_code=issue.device_code, client_id="client-one", now=105),
    )
    statuses = {first.status, second.status}
    assert statuses == {DEVICE_POLL_APPROVED, DEVICE_POLL_REPLAYED}
    approved = first if first.approved else second
    assert approved.authorization == {
        "sub": "user-1",
        "registry_access_id": "aut_one",
    }
    stored = "\n".join(redis.values.values())
    assert "registry_access_id" not in stored


@pytest.mark.asyncio
async def test_denial_expiry_and_replay_have_stable_outcomes():
    _, store = _store(ttl=10)
    denied = await store.create(client_id="client-one", scopes=[], now=100)
    lookup = await store.read_user_code(
        denied.user_code,
        attempt_key="browser",
        now=101,
    )
    assert await store.deny(
        device_digest=lookup.device_digest,
        user_digest=lookup.user_digest,
        approving_subject="user-1",
        now=101,
    ) == "denied"
    refusal = await store.poll(
        device_code=denied.device_code,
        client_id="client-one",
        now=105,
    )
    assert refusal.status == DEVICE_POLL_ACCESS_DENIED
    replay = await store.poll(
        device_code=denied.device_code,
        client_id="client-one",
        now=106,
    )
    assert replay.status == DEVICE_POLL_REPLAYED

    expired = await store.create(client_id="client-one", scopes=[], now=200)
    result = await store.poll(
        device_code=expired.device_code,
        client_id="client-one",
        now=210,
    )
    assert result.status == DEVICE_POLL_EXPIRED


@pytest.mark.asyncio
async def test_card_revision_conflict_is_a_distinct_terminal_outcome():
    _, store = _store()
    issue = await store.create(client_id="client-one", scopes=[], now=100)
    lookup = await store.read_user_code(
        issue.user_code,
        attempt_key="browser",
        now=101,
    )
    assert await store.deny(
        device_digest=lookup.device_digest,
        user_digest=lookup.user_digest,
        approving_subject="user-1",
        error="device_card_revision_conflict",
        now=101,
    ) == "denied"
    result = await store.poll(
        device_code=issue.device_code,
        client_id="client-one",
        now=105,
    )
    assert result.status == "device_card_revision_conflict"


@pytest.mark.asyncio
async def test_wrong_user_code_is_rate_limited_without_disclosing_a_request():
    _, store = _store(attempts=2)
    first = await store.read_user_code("BCDF-GHJK", attempt_key="source-one")
    second = await store.read_user_code("BCDF-GHJK", attempt_key="source-one")
    third = await store.read_user_code("BCDF-GHJK", attempt_key="source-one")
    assert first.status == second.status == "not_found"
    assert third.status == "rate_limited"
    assert third.request is None


@pytest.mark.asyncio
async def test_rate_limited_browser_cannot_bypass_limit_with_valid_code():
    _, store = _store(attempts=2)
    issue = await store.create(client_id="client-one", scopes=[], now=100)
    for value in ("BCDF-GHJK", "MNPQ-RTVW"):
        miss = await store.read_user_code(value, attempt_key="source-one", now=101)
        assert miss.status == "not_found"
    blocked = await store.read_user_code(
        issue.user_code,
        attempt_key="source-one",
        now=101,
    )
    assert blocked.status == "rate_limited"


@pytest.mark.asyncio
async def test_approval_requires_the_user_code_to_match_the_device_request():
    _, store = _store()
    first = await store.create(client_id="client-one", scopes=[], now=100)
    second = await store.create(client_id="client-one", scopes=[], now=100)
    lookup = await store.read_user_code(first.user_code, attempt_key="browser", now=101)
    assert lookup.status == "found"
    assert await store.approve(
        device_digest=lookup.device_digest,
        user_digest=user_code_digest(second.user_code),
        approving_subject="user-1",
        authorization={"sub": "user-1"},
        now=101,
    ) == "not_found"


@pytest.mark.asyncio
async def test_browser_lookup_rejects_logically_expired_request():
    redis, store = _store(ttl=10)
    issue = await store.create(client_id="client-one", scopes=[], now=100)
    result = await store.read_user_code(
        issue.user_code,
        attempt_key="browser",
        now=110,
    )
    assert result.status == "not_found"
    assert not any(":device:user:" in key for key in redis.values)


def test_device_record_rejects_nested_token_material():
    with pytest.raises(ValueError, match="device_record_secret_field"):
        assert_device_record_safe(
            {"authorization": {"nested": {"refresh_token": "must-not-land"}}}
        )


def test_authorization_metadata_advertises_device_grant_and_endpoint():
    metadata = authorization_server_metadata("https://hub.example.test")
    assert metadata["device_authorization_endpoint"] == (
        "https://hub.example.test/oauth/device_authorization"
    )
    assert (
        "urn:ietf:params:oauth:grant-type:device_code"
        in metadata["grant_types_supported"]
    )


@pytest.mark.asyncio
async def test_real_redis_allows_exactly_one_device_code_consumer():
    redis_url = os.environ.get("REDIS_URL") or ""
    if not redis_url:
        pytest.skip("REDIS_URL is not set; real-Redis device Lua is skipped")

    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(redis_url)
    tenant = f"device-test-{uuid.uuid4().hex[:10]}"
    project = f"project-{uuid.uuid4().hex[:10]}"
    prefix = f"{tenant}:{project}:kdcube:oauth:device:"
    connected = False
    try:
        try:
            await client.ping()
        except Exception as exc:  # pragma: no cover - environment guard
            pytest.skip(f"Redis at REDIS_URL is unusable: {exc}")
        connected = True
        store = DeviceGrantStore(client, tenant, project, request_ttl=60)
        issue = await store.create(client_id="client-one", scopes=[], now=100)
        lookup = await store.read_user_code(
            issue.user_code,
            attempt_key="browser-session",
            now=101,
        )
        assert lookup.status == "found"
        assert await store.approve(
            device_digest=lookup.device_digest,
            user_digest=lookup.user_digest,
            approving_subject="user-1",
            authorization={"sub": "user-1", "registry_access_id": "aut_one"},
            now=101,
        ) == "approved"

        first, second = await asyncio.gather(
            store.poll(
                device_code=issue.device_code,
                client_id="client-one",
                now=105,
            ),
            store.poll(
                device_code=issue.device_code,
                client_id="client-one",
                now=105,
            ),
        )
        assert {first.status, second.status} == {
            DEVICE_POLL_APPROVED,
            DEVICE_POLL_REPLAYED,
        }
    finally:
        if connected:
            keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
            if keys:
                await client.delete(*keys)
        await client.aclose()

from __future__ import annotations

import asyncio
import json

import pytest

from connection_hub.one_time_state import (
    ONE_TIME_POINTER_SCHEMA,
    OneTimeStateError,
    RedisRunBoundPointerStore,
    SecretBackedOneTimeStateStore,
    state_digest,
)


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str]] = []

    def info(self, section: str) -> "_Pipeline":
        self._operations.append(("info", section))
        return self

    def get(self, key: str) -> "_Pipeline":
        self._operations.append(("get", key))
        return self

    def delete(self, key: str) -> "_Pipeline":
        self._operations.append(("delete", key))
        return self

    async def execute(self) -> list[object]:
        async with self._redis.lock:
            values: list[object] = []
            for operation, argument in self._operations:
                if operation == "info":
                    values.append({"run_id": self._redis.run_id})
                elif operation == "get":
                    values.append(self._redis.values.get(argument))
                else:
                    values.append(1 if self._redis.values.pop(argument, None) is not None else 0)
            return values


class _Redis:
    def __init__(self, *, run_id: str = "run-a") -> None:
        self.run_id = run_id
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.fail_set = False

    async def info(self, section: str) -> dict[str, str]:
        assert section == "server"
        return {"run_id": self.run_id}

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex > 0
        if self.fail_set:
            raise RuntimeError("redis unavailable")
        self.values[key] = value
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}
        self.deleted: list[str] = []
        self.fail_delete = False

    async def set(self, *, secret_ref: str, value: str, expires_at: int) -> None:
        self.values[secret_ref] = (value, expires_at)

    async def get(self, *, secret_ref: str) -> str | None:
        item = self.values.get(secret_ref)
        return item[0] if item is not None else None

    async def delete(self, *, secret_ref: str) -> None:
        if self.fail_delete:
            raise RuntimeError("secret delete unavailable")
        self.values.pop(secret_ref, None)
        self.deleted.append(secret_ref)

    async def purge_expired(self, *, now: int, limit: int) -> int:
        expired = [
            secret_ref
            for secret_ref, (_value, expires_at) in self.values.items()
            if expires_at <= now
        ][:limit]
        for secret_ref in expired:
            await self.delete(secret_ref=secret_ref)
        return len(expired)


def _store(
    redis: _Redis | None = None,
    secrets: _Secrets | None = None,
) -> tuple[SecretBackedOneTimeStateStore, _Redis, _Secrets]:
    resolved_redis = redis or _Redis()
    resolved_secrets = secrets or _Secrets()
    return (
        SecretBackedOneTimeStateStore(
            pointers=RedisRunBoundPointerStore(
                resolved_redis,
                prefix="test:one-time-state",
            ),
            secrets=resolved_secrets,
            purpose="browser-login",
        ),
        resolved_redis,
        resolved_secrets,
    )


@pytest.mark.asyncio
async def test_redis_contains_only_a_run_bound_non_secret_pointer() -> None:
    store, redis, secrets = _store()

    await store.put(
        "browser-state",
        {"code_verifier": "pkce-secret", "next_path": "/app"},
        ttl_seconds=120,
        now=100,
    )

    assert list(redis.values) == [f"test:one-time-state:{state_digest('browser-state')}"]
    pointer = json.loads(next(iter(redis.values.values())))
    assert pointer == {
        "created_at": 100,
        "expires_at": 220,
        "purpose": "browser-login",
        "redis_run_id": "run-a",
        "schema": ONE_TIME_POINTER_SCHEMA,
        "secret_ref": pointer["secret_ref"],
        "state_digest": state_digest("browser-state"),
    }
    assert "browser-state" not in json.dumps(pointer)
    assert "pkce-secret" not in json.dumps(pointer)
    secret = json.loads(secrets.values[pointer["secret_ref"]][0])
    assert secret["state"] == "browser-state"
    assert secret["payload"]["code_verifier"] == "pkce-secret"


@pytest.mark.asyncio
async def test_claim_is_single_use_and_deletes_the_secret() -> None:
    store, redis, secrets = _store()
    await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)

    first, second = await asyncio.gather(
        store.pop("state", now=101),
        store.pop("state", now=101),
    )

    assert sorted([first, second], key=lambda value: value is None) == [
        {"nonce": "n"},
        None,
    ]
    assert redis.values == {}
    assert secrets.values == {}
    assert len(secrets.deleted) == 1


@pytest.mark.asyncio
async def test_restored_pointer_from_an_older_redis_run_is_consumed_not_served() -> None:
    store, redis, secrets = _store()
    await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)
    redis.run_id = "run-after-restore"

    assert await store.pop("state", now=101) is None
    assert redis.values == {}
    assert secrets.values == {}


@pytest.mark.asyncio
async def test_pointer_write_failure_compensates_the_secret() -> None:
    redis = _Redis()
    redis.fail_set = True
    store, _redis, secrets = _store(redis=redis)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)

    assert secrets.values == {}
    assert len(secrets.deleted) == 1


@pytest.mark.asyncio
async def test_pointer_error_survives_a_failed_compensating_delete() -> None:
    redis = _Redis()
    redis.fail_set = True
    secrets = _Secrets()
    secrets.fail_delete = True
    store, _redis, _secrets = _store(redis=redis, secrets=secrets)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)

    assert len(secrets.values) == 1, "expiry purge owns the orphaned secret"


@pytest.mark.asyncio
async def test_successful_claim_survives_a_failed_secret_cleanup() -> None:
    store, _redis, secrets = _store()
    await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)
    secrets.fail_delete = True

    assert await store.pop("state", now=101) == {"nonce": "n"}


@pytest.mark.asyncio
async def test_legacy_pointer_is_consumed_as_an_expired_attempt() -> None:
    store, redis, secrets = _store()
    digest = state_digest("legacy-state")
    redis.values[f"test:one-time-state:{digest}"] = json.dumps(
        {
            "redis_run_id": redis.run_id,
            "state": "legacy-state",
            "payload": {"nonce": "old"},
        }
    )

    assert await store.pop("legacy-state", now=101) is None
    assert redis.values == {}
    assert secrets.values == {}


@pytest.mark.asyncio
async def test_missing_redis_run_id_refuses_creation() -> None:
    store, _redis, secrets = _store(redis=_Redis(run_id=""))

    with pytest.raises(
        OneTimeStateError,
        match="one_time_state_redis_run_id_unavailable",
    ):
        await store.put("state", {"nonce": "n"}, ttl_seconds=120, now=100)

    assert secrets.values == {}

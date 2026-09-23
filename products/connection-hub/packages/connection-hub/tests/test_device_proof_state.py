from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from connection_hub.delegated_credentials.devices import (
    DeviceProofStateError,
    RedisDeviceProofState,
)


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.operations: list[tuple[str, str]] = []

    def info(self, section: str) -> "_Pipeline":
        self.operations.append(("info", section))
        return self

    def get(self, key: str) -> "_Pipeline":
        self.operations.append(("get", key))
        return self

    def delete(self, key: str) -> "_Pipeline":
        self.operations.append(("delete", key))
        return self

    async def execute(self) -> list[Any]:
        async with self.redis.lock:
            values: list[Any] = []
            for operation, argument in self.operations:
                if operation == "info":
                    values.append({"run_id": self.redis.run_id})
                elif operation == "get":
                    values.append(self.redis.values.get(argument))
                else:
                    values.append(
                        int(self.redis.values.pop(argument, None) is not None)
                    )
            return values


class _Redis:
    def __init__(self) -> None:
        self.run_id = "run-a"
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def info(self, section: str) -> dict[str, str]:
        assert section == "server"
        return {"run_id": self.run_id}

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)


@pytest.mark.asyncio
async def test_proof_nonce_is_one_use_and_redis_run_bound() -> None:
    redis = _Redis()
    state = RedisDeviceProofState(
        redis,
        tenant="demo-tenant",
        project="demo-project",
    )
    nonce = await state.issue_nonce(purpose="recovery.poll")

    await state.claim(nonce=nonce, jti="proof-1", purpose="recovery.poll")
    with pytest.raises(DeviceProofStateError, match="device_proof_nonce_unknown"):
        await state.claim(nonce=nonce, jti="proof-2", purpose="recovery.poll")

    restored_nonce = await state.issue_nonce(purpose="recovery.poll")
    redis.run_id = "run-after-restore"
    with pytest.raises(
        DeviceProofStateError,
        match="device_proof_nonce_generation_changed",
    ):
        await state.claim(
            nonce=restored_nonce,
            jti="proof-after-restore",
            purpose="recovery.poll",
        )


@pytest.mark.asyncio
async def test_proof_replay_id_is_reserved_across_nonces() -> None:
    redis = _Redis()
    state = RedisDeviceProofState(
        redis,
        tenant="demo-tenant",
        project="demo-project",
    )
    first = await state.issue_nonce(purpose="package.fetch")
    second = await state.issue_nonce(purpose="package.fetch")

    await state.claim(nonce=first, jti="same-proof", purpose="package.fetch")
    with pytest.raises(DeviceProofStateError, match="device_proof_replayed"):
        await state.claim(
            nonce=second,
            jti="same-proof",
            purpose="package.fetch",
        )

    assert any(
        json.loads(value).get("purpose") == "package.fetch"
        for key, value in redis.values.items()
        if ":jti:" in key
    )

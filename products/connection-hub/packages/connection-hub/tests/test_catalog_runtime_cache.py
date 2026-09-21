from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.catalog.runtime_cache import (
    DelegatedCatalogRuntimeCache,
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

    async def execute(self) -> list[object]:
        return [
            {"run_id": self.redis.run_id}
            if operation == "info"
            else self.redis.values.get(argument)
            for operation, argument in self.operations
        ]


class _Redis:
    def __init__(self) -> None:
        self.run_id = "run-a"
        self.values: dict[str, str] = {}

    async def info(self, section: str) -> dict[str, str]:
        assert section == "server"
        return {"run_id": self.run_id}

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int):
        assert ex > 0
        self.values[key] = value
        return True

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        key: str,
        encoded: str,
        version: str,
        allow_equal: str,
        ttl: str,
        run_id: str,
    ) -> int:
        assert int(ttl) > 0
        existing = json.loads(self.values[key]) if key in self.values else None
        if isinstance(existing, dict) and existing.get("redis_run_id") == run_id:
            existing_version = str(existing.get("version") or "")
            if existing_version > version:
                return 0
            if existing_version == version and allow_equal != "1":
                return 0
        self.values[key] = encoded
        return 1


def _document(day: int, marker: str) -> CatalogDocument:
    return CatalogDocument.build(
        {"service": {"marker": marker}},
        created_at=datetime(2026, 9, day, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_active_catalog_is_visible_only_in_the_redis_run_that_published_it():
    redis = _Redis()
    cache = DelegatedCatalogRuntimeCache(redis, tenant="tenant", project="project")
    document = _document(20, "current")

    assert await cache.publish_active(document, ttl_seconds=300) is True
    assert await cache.read_active() == document

    redis.run_id = "run-after-restore"

    assert await cache.read_active() is None


@pytest.mark.asyncio
async def test_durable_active_catalog_replaces_a_newer_version_from_an_old_run():
    redis = _Redis()
    cache = DelegatedCatalogRuntimeCache(redis, tenant="tenant", project="project")
    durable = _document(20, "durable")
    restored_newer = _document(21, "snapshot")
    assert restored_newer.version > durable.version
    assert await cache.publish_active(restored_newer, ttl_seconds=300) is True

    redis.run_id = "run-after-restore"

    assert await cache.restore_active(durable, ttl_seconds=300) is True
    assert await cache.read_active() == durable


@pytest.mark.asyncio
async def test_same_run_catalog_compare_and_set_still_rejects_an_older_restore():
    redis = _Redis()
    cache = DelegatedCatalogRuntimeCache(redis, tenant="tenant", project="project")
    older = _document(20, "older")
    newer = _document(21, "newer")

    assert await cache.publish_active(newer, ttl_seconds=300) is True
    assert await cache.restore_active(older, ttl_seconds=300) is False
    assert await cache.read_active() == newer

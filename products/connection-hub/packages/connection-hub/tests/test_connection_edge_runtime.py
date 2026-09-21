from __future__ import annotations

import json

import pytest

from connection_hub.hub.edge_cache import (
    ConnectionEdgeRuntimeCache,
    ConnectionEdgeRuntimeCacheError,
)
from connection_hub.hub.edges import ConnectionEdgeStore, ConnectionEdgeStoreError
from connection_hub.hub.resolver import resolve_identity_family


class _Pipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.operations: list[tuple[str, str]] = []

    def info(self, section: str) -> "_Pipeline":
        self.operations.append(("info", section))
        return self

    def get(self, key: str) -> "_Pipeline":
        self.operations.append(("get", key))
        return self

    async def execute(self):
        return [
            {"run_id": self.redis.run_id}
            if operation == "info"
            else self.redis.values.get(argument)
            for operation, argument in self.operations
        ]


class FakeRedis:
    def __init__(self) -> None:
        self.run_id = "run-a"
        self.values: dict[str, str] = {}

    async def info(self, section: str):
        assert section == "server"
        return {"run_id": self.run_id}

    def pipeline(self, *, transaction: bool):
        assert transaction is True
        return _Pipeline(self)

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str):
        return int(self.values.pop(key, None) is not None)

    async def eval(self, _script: str, _numkeys: int, key: str, *args: str):
        if len(args) == 1:
            [token] = args
            if self.values.get(key) != token:
                return 0
            return await self.delete(key)
        encoded, revision, run_id = args
        existing = json.loads(self.values[key]) if key in self.values else None
        if (
            isinstance(existing, dict)
            and existing.get("redis_run_id") == run_id
        ):
            existing_revision = int(existing.get("edge_revision") or 0)
            if existing_revision > int(revision):
                return 0
            if existing_revision == int(revision):
                return 1 if self.values[key] == encoded else -1
        self.values[key] = encoded
        return 1


def test_edge_resolution_is_scoped_to_the_source_authority(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    first = store.upsert_edge(
        from_authority_id="https://issuer-a.example",
        from_provider="oidc",
        from_subject="same-sub",
        to_user_id="oidc:first",
    )
    store.upsert_edge(
        from_authority_id="https://issuer-b.example",
        from_provider="oidc",
        from_subject="same-sub",
        to_user_id="oidc:second",
    )

    resolved = store.resolve_edge(
        from_authority_id="https://issuer-a.example",
        from_provider="oidc",
        from_subject="same-sub",
    )

    assert resolved == first


def test_edge_store_rejects_two_targets_for_one_source_identity(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    store.upsert_edge(
        from_authority_id="https://issuer.example",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:first",
    )

    with pytest.raises(ValueError, match="already targets another identity"):
        store.upsert_edge(
            from_authority_id="https://issuer.example",
            from_provider="oidc",
            from_subject="subject",
            to_user_id="oidc:second",
        )


def test_remove_is_scoped_to_the_source_authority(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    for issuer, target in (("issuer-a", "oidc:first"), ("issuer-b", "oidc:second")):
        store.upsert_edge(
            from_authority_id=issuer,
            from_provider="oidc",
            from_subject="same-sub",
            to_user_id=target,
        )

    removed = store.remove_edge(
        from_authority_id="issuer-a",
        from_provider="oidc",
        from_subject="same-sub",
    )

    assert [edge["to"]["user_id"] for edge in removed["edges"]] == ["oidc:first"]
    assert store.resolve_edge(
        from_authority_id="issuer-b",
        from_provider="oidc",
        from_subject="same-sub",
    )["to"]["user_id"] == "oidc:second"


def test_edge_store_advances_a_durable_revision_for_each_mutation(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    first = store.upsert_edge(
        from_authority_id="issuer-a",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:first",
    )
    second = store.upsert_edge(
        from_authority_id="issuer-a",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:first",
        label="Updated",
    )
    removed = store.remove_edge(
        from_authority_id="issuer-a",
        from_provider="oidc",
        from_subject="subject",
    )

    assert first["store_revision"] == 1
    assert second["store_revision"] == 2
    assert removed["store_revision"] == 3


def test_prefixed_platform_principal_is_not_reparsed_as_an_external_actor(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    store.upsert_edge(
        from_authority_id="https://issuer.example",
        from_provider="cognito",
        from_subject="subject",
        to_user_id="cognito:subject",
    )

    family = resolve_identity_family(
        store,
        platform_user_id="cognito:subject",
    )

    assert family["linked"] is True
    assert family["platform_user_id"] == "cognito:subject"
    assert family["input"].get("provider") is None
    assert family["user_ids"][0] == "cognito:subject"


def test_edge_store_does_not_treat_corrupt_authority_as_empty(tmp_path):
    path = tmp_path / "connections" / "connection-edges.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ConnectionEdgeStoreError, match="valid JSON"):
        ConnectionEdgeStore(tmp_path).resolve_edge(
            from_provider="oidc",
            from_subject="subject",
        )


def test_edge_store_does_not_treat_corrupt_challenges_as_empty(tmp_path):
    path = tmp_path / "connections" / "connection-edge-challenges.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ConnectionEdgeStoreError, match="valid JSON"):
        ConnectionEdgeStore(tmp_path).get_edge_challenge(challenge_id="challenge")


@pytest.mark.asyncio
async def test_redis_projection_round_trips_and_removes_an_edge(tmp_path):
    edge = ConnectionEdgeStore(tmp_path).upsert_edge(
        from_authority_id="https://issuer.example",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:subject",
    )
    redis = FakeRedis()
    cache = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")

    published = await cache.publish_edge(edge)
    assert published["platform_user_id"] == "oidc:subject"
    assert await cache.read(
        authority_id="https://issuer.example",
        subject="subject",
    ) == published

    assert await cache.remove(
        authority_id="https://issuer.example",
        subject="subject",
    ) is True
    assert await cache.read(
        authority_id="https://issuer.example",
        subject="subject",
    ) is None


@pytest.mark.asyncio
async def test_redis_projection_ignores_invalid_cached_json():
    redis = FakeRedis()
    cache = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")
    key = cache.key(authority_id="issuer", subject="subject")
    redis.values[key] = json.dumps({"schema": "wrong"})

    assert await cache.read(authority_id="issuer", subject="subject") is None


@pytest.mark.asyncio
async def test_redis_projection_from_an_older_run_is_not_authority(tmp_path):
    edge = ConnectionEdgeStore(tmp_path).upsert_edge(
        from_authority_id="issuer",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:subject",
    )
    redis = FakeRedis()
    cache = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")
    await cache.publish_edge(edge)

    redis.run_id = "run-after-restore"

    assert await cache.read(authority_id="issuer", subject="subject") is None


@pytest.mark.asyncio
async def test_redis_projection_returns_the_readable_newer_revision(tmp_path):
    store = ConnectionEdgeStore(tmp_path)
    older = store.upsert_edge(
        from_authority_id="issuer",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:subject",
    )
    newer = store.upsert_edge(
        from_authority_id="issuer",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:subject",
        label="newer",
    )
    redis = FakeRedis()
    cache = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")
    expected = await cache.publish_edge(newer)

    assert await cache.publish_edge(older) == expected


@pytest.mark.asyncio
async def test_redis_projection_rejects_different_content_at_one_revision(tmp_path):
    edge = ConnectionEdgeStore(tmp_path).upsert_edge(
        from_authority_id="issuer",
        from_provider="oidc",
        from_subject="subject",
        to_user_id="oidc:subject",
    )
    redis = FakeRedis()
    cache = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")
    await cache.publish_edge(edge)
    conflict = json.loads(json.dumps(edge))
    conflict["to"]["user_id"] = "oidc:different"

    with pytest.raises(
        ConnectionEdgeRuntimeCacheError,
        match="same revision",
    ):
        await cache.publish_edge(conflict)


@pytest.mark.asyncio
async def test_mutation_lock_is_shared_by_the_tenant_project_and_released():
    redis = FakeRedis()
    first = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")
    second = ConnectionEdgeRuntimeCache(redis, tenant="tenant", project="project")

    assert first.mutation_lock_key() == second.mutation_lock_key()
    async with first.mutation_lock():
        assert first.mutation_lock_key() in redis.values

    assert first.mutation_lock_key() not in redis.values

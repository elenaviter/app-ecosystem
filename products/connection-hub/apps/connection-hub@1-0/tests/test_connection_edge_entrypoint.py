from __future__ import annotations

from pathlib import Path

import pytest

from connection_hub.hub.edges import ConnectionEdgeStore
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str):
        return int(self.values.pop(key, None) is not None)

    async def eval(self, _script: str, _numkeys: int, key: str, token: str):
        if self.values.get(key) != token:
            return 0
        return await self.delete(key)


class ProjectionDeleteFailRedis(FakeRedis):
    async def delete(self, key: str):
        if ":principal:" in key:
            raise RuntimeError("redis unavailable")
        return await super().delete(key)


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


@pytest.mark.asyncio
async def test_edge_operations_update_the_shared_principal_projection(
    tmp_path,
    monkeypatch,
):
    module = _entrypoint_module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = FakeRedis()
    store = ConnectionEdgeStore(tmp_path)
    monkeypatch.setattr(module, "_edge_store", lambda _entrypoint: store)
    monkeypatch.setattr(
        module,
        "_runtime_tenant_project",
        lambda _entrypoint: ("tenant", "project"),
    )
    monkeypatch.setattr(
        module,
        "_platform_user_id",
        lambda _entrypoint, *, user_id=None: str(user_id or ""),
    )
    monkeypatch.setattr(module, "_identity_config", lambda _entrypoint: {})

    created = await module.ConnectionHubEntrypoint.connection_edge_upsert(
        instance,
        authority_id="https://issuer.example",
        provider="oidc",
        provider_subject="subject-1",
        platform_user_id="oidc:subject-1",
        user_id="oidc:subject-1",
    )

    assert created["ok"] is True, created
    cached = await module._edge_runtime_cache(instance).read(
        authority_id="https://issuer.example",
        subject="subject-1",
    )
    assert cached["platform_user_id"] == "oidc:subject-1"

    projection_key = module._edge_runtime_cache(instance).key(
        authority_id="https://issuer.example",
        subject="subject-1",
    )
    remove_edge = store.remove_edge

    def assert_projection_invalidated_before_remove(**kwargs):
        assert projection_key not in instance.redis.values
        return remove_edge(**kwargs)

    monkeypatch.setattr(store, "remove_edge", assert_projection_invalidated_before_remove)

    removed = await module.ConnectionHubEntrypoint.connection_edge_remove(
        instance,
        authority_id="https://issuer.example",
        provider="oidc",
        provider_subject="subject-1",
        user_id="oidc:subject-1",
    )

    assert removed["ok"] is True
    assert await module._edge_runtime_cache(instance).read(
        authority_id="https://issuer.example",
        subject="subject-1",
    ) is None
    assert module._edge_runtime_cache(instance).mutation_lock_key() not in instance.redis.values


@pytest.mark.asyncio
async def test_edge_mutation_fails_closed_without_shared_redis(tmp_path, monkeypatch):
    module = _entrypoint_module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    store = ConnectionEdgeStore(tmp_path)
    monkeypatch.setattr(module, "_edge_store", lambda _entrypoint: store)
    monkeypatch.setattr(
        module,
        "_platform_user_id",
        lambda _entrypoint, *, user_id=None: str(user_id or ""),
    )

    result = await module.ConnectionHubEntrypoint.connection_edge_upsert(
        instance,
        authority_id="https://issuer.example",
        provider="oidc",
        provider_subject="subject-1",
        platform_user_id="oidc:subject-1",
        user_id="oidc:subject-1",
    )

    assert result["error"] == "connection_edge_coordination_unavailable"
    assert store.list_edges() == []


@pytest.mark.asyncio
async def test_edge_remove_keeps_durable_edge_when_projection_cannot_be_invalidated(
    tmp_path,
    monkeypatch,
):
    module = _entrypoint_module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = ProjectionDeleteFailRedis()
    store = ConnectionEdgeStore(tmp_path)
    edge = store.upsert_edge(
        from_authority_id="https://issuer.example",
        from_provider="oidc",
        from_subject="subject-1",
        to_user_id="oidc:subject-1",
    )
    monkeypatch.setattr(module, "_edge_store", lambda _entrypoint: store)
    monkeypatch.setattr(
        module,
        "_runtime_tenant_project",
        lambda _entrypoint: ("tenant", "project"),
    )
    monkeypatch.setattr(
        module,
        "_platform_user_id",
        lambda _entrypoint, *, user_id=None: str(user_id or ""),
    )
    await module._project_connection_edge(instance, edge)

    result = await module.ConnectionHubEntrypoint.connection_edge_remove(
        instance,
        authority_id="https://issuer.example",
        provider="oidc",
        provider_subject="subject-1",
        user_id="oidc:subject-1",
    )

    assert result["error"] == "connection_edge_coordination_unavailable"
    assert store.resolve_edge(
        from_authority_id="https://issuer.example",
        from_provider="oidc",
        from_subject="subject-1",
    ) is not None


@pytest.mark.asyncio
async def test_challenge_status_resolves_the_edge_in_its_source_authority(
    tmp_path,
    monkeypatch,
):
    module = _entrypoint_module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    store = ConnectionEdgeStore(tmp_path)
    for authority_id, target in (
        ("https://issuer-a.example", "oidc:first"),
        ("https://issuer-b.example", "oidc:second"),
    ):
        store.upsert_edge(
            from_authority_id=authority_id,
            from_provider="oidc",
            from_subject="same-subject",
            to_user_id=target,
        )
    challenge = store.create_provider_claim_challenge(
        provider="oidc",
        provider_subject="same-subject",
        metadata={"authority_id": "https://issuer-a.example"},
    )
    monkeypatch.setattr(module, "_edge_store", lambda _entrypoint: store)
    monkeypatch.setattr(
        module,
        "_platform_user_id",
        lambda _entrypoint, *, user_id=None: str(user_id or ""),
    )
    monkeypatch.setattr(
        module,
        "_platform_delegation_grant_options",
        lambda _entrypoint, _user_id: [],
    )

    result = await module.ConnectionHubEntrypoint.connection_edge_challenge_status(
        instance,
        challenge_id=challenge["challenge_id"],
        user_id="oidc:first",
    )

    assert result["ok"] is True
    assert result["edge"]["to"]["user_id"] == "oidc:first"

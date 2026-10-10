"""W661 option P: the Hub's bundle load activates the Card pointer/marker rows (C1), one-way, from a bundle prop.

The property is connections.delegated_credentials.lifecycle_storage.card_rows = "postgresql" (kdcube docs
sdk/bundle/bundle-properties-and-secrets-lifecycle-README.md: a feature toggle is a descriptor property). Real
PostgreSQL rows (W661_TEST_PG_DSN); the entrypoint's config readers are monkeypatched to a synthetic scope.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

DSN = os.environ.get("W661_TEST_PG_DSN", "")


def _module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


def _host(module, monkeypatch, tmp_path, *, configured, project):
    storage = {"card_rows": "postgresql"} if configured else {}
    monkeypatch.setattr(module, "_connections_config",
                        lambda _e: {"delegated_credentials": {"lifecycle_storage": storage}})
    monkeypatch.setattr(module, "_runtime_tenant_project", lambda _e: ("t", project))
    monkeypatch.setattr(module, "_storage_root_or_error", lambda _e: tmp_path)
    return SimpleNamespace()


def test_configured_without_a_pg_pool_refuses_the_load(monkeypatch, tmp_path):
    module = _module()
    host = _host(module, monkeypatch, tmp_path, configured=True, project="p")
    with pytest.raises(RuntimeError, match="card_rows_require_pg_pool"):
        asyncio.run(module._activate_card_rows(host, None))
    assert asyncio.run(module._activate_card_rows(_host(module, monkeypatch, tmp_path, configured=False,
                                                        project="p"), None)) is False


@pytest.mark.skipif(not DSN, reason="W661_TEST_PG_DSN is not set")
def test_activation_attaches_the_rows_to_every_card_store_and_is_one_way(monkeypatch, tmp_path):
    import asyncpg

    from connection_hub.hub.authenticator_store import schema_for_scope

    module = _module()
    project = "p" + uuid.uuid4().hex[:10]
    monkeypatch.setattr(module, "_CARD_ROWS_BY_ROOT", {})

    async def run():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            # Not configured, never activated: file-only stores, and no DDL in this scope (Ops' N3).
            assert await module._activate_card_rows(
                _host(module, monkeypatch, tmp_path, configured=False, project=project), pool) is False
            assert getattr(module._guarded_card_store(tmp_path), "card_rows", None) is None
            schema = schema_for_scope(tenant="t", project=project)
            async with pool.acquire() as connection:
                assert await connection.fetchval("SELECT to_regclass($1)",
                                                 f"{schema}.connection_hub_card_pointer_authority") is None
            # Configured: activated, and every Card store over this root gets the rows.
            assert await module._activate_card_rows(
                _host(module, monkeypatch, tmp_path, configured=True, project=project), pool) is True
            assert module._guarded_card_store(tmp_path).card_rows is not None
            # One-way: a later load without the property still uses the rows of the activated scope.
            module._CARD_ROWS_BY_ROOT.clear()
            assert await module._activate_card_rows(
                _host(module, monkeypatch, tmp_path, configured=False, project=project), pool) is True
            assert module._guarded_card_store(tmp_path).card_rows is not None
        finally:
            await pool.close()

    asyncio.run(run())

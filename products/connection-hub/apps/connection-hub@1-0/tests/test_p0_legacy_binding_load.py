"""P0, 10 Oct 2026: the Hub's bundle load runs the legacy project binding repair, and never fails on it."""
import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


@pytest.mark.asyncio
async def test_the_repair_gets_the_hub_service_and_a_card_store_at_the_bundle_storage_root(monkeypatch, tmp_path):
    m = module()
    service, calls = object(), []

    async def automation_access_service(entrypoint, request):
        assert request is None
        return service

    async def repair(host, store):
        calls.append((host, store))
        return {"c_bound": 7}

    import connection_hub.delegated_credentials.legacy_binding_repair as repair_module
    monkeypatch.setattr(m, "_automation_access_service", automation_access_service)
    monkeypatch.setattr(m, "_lifecycle_lock_scope", lambda entrypoint: "")
    monkeypatch.setattr(repair_module, "repair_legacy_project_bindings", repair)
    entry = SimpleNamespace(bundle_storage_root=lambda: tmp_path)
    assert await m._repair_legacy_project_bindings(entry) == {"c_bound": 7}
    ((host, store),) = calls
    assert host is service and isinstance(store, m.BundleStorageDelegatedCardStore)
    assert store.root == m.BundleStorageDelegatedCardStore(tmp_path).root  # the same store the Hub persists to


@pytest.mark.asyncio
async def test_without_bundle_storage_nothing_is_repaired(monkeypatch):
    m = module()
    entry = SimpleNamespace(bundle_storage_root=lambda: None)
    assert await m._repair_legacy_project_bindings(entry) == {}


def test_bundle_load_runs_the_repair_and_a_failure_never_blocks_the_load():
    m = module()
    source = inspect.getsource(m.ConnectionHubEntrypoint.on_bundle_load)
    tree = ast.parse(source.strip().replace("async def", "async def", 1))
    guarded = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
               and any("_repair_legacy_project_bindings" in ast.unparse(statement) for statement in node.body)]
    assert guarded, "on_bundle_load must call the repair inside try"
    handler = guarded[0].handlers[0]
    assert ast.unparse(handler.type) == "Exception"
    assert not any(isinstance(node, ast.Raise) for node in ast.walk(handler)), "a failure must not block the load"

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
    monkeypatch.setenv("GATEWAY_COMPONENT", "proc")  # W661 K1: Cards are written only in chat-proc
    entry = SimpleNamespace(bundle_storage_root=lambda: tmp_path)
    assert await m._repair_legacy_project_bindings(entry) == {"c_bound": 7}
    ((host, store),) = calls
    assert host is service and isinstance(store, m.BundleStorageDelegatedCardStore)
    assert store.root == m.BundleStorageDelegatedCardStore(tmp_path).root  # the same store the Hub persists to


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ingress", None, "openapi"])
async def test_the_repair_is_skipped_outside_chat_proc(monkeypatch, tmp_path, role):
    """W661 K1 (EMain 18:40Z): chat-ingress loads this bundle for request_authenticate; it never repairs Cards."""
    m = module()
    calls = []

    async def repair(host, store):
        calls.append(store)
        return {"c_bound": 1}

    import connection_hub.delegated_credentials.legacy_binding_repair as repair_module
    monkeypatch.setattr(repair_module, "repair_legacy_project_bindings", repair)
    if role is None:
        monkeypatch.delenv("GATEWAY_COMPONENT", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_COMPONENT", role)
    assert await m._repair_legacy_project_bindings(SimpleNamespace(bundle_storage_root=lambda: tmp_path)) == {}
    assert calls == []


@pytest.mark.asyncio
async def test_the_hub_card_persistence_takes_the_proc_only_container_local_lock(monkeypatch, tmp_path):
    """W661 K1: the Card persistence the Hub builds refuses a Card lock outside proc, and takes it on a local
    file under the configured root inside proc."""
    m = module()
    root = tmp_path / "local-locks"
    monkeypatch.setattr(m, "_card_lock_root", lambda entrypoint: str(root))
    lock = m._card_mutation_lock(SimpleNamespace())
    shared = tmp_path / "share" / "cards" / "aut_a" / ".mutation.lock"
    monkeypatch.setenv("GATEWAY_COMPONENT", "ingress")
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
        async with lock(lock_path=shared, resource_id="delegated-card:aut_a", operation="t", wait_seconds=1):
            pass
    monkeypatch.setenv("GATEWAY_COMPONENT", "proc")
    async with lock(lock_path=shared, resource_id="delegated-card:aut_a", operation="t", wait_seconds=1):
        assert [p.suffix for p in root.iterdir()] == [".lock"]
    assert not shared.exists()  # nothing on the share


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

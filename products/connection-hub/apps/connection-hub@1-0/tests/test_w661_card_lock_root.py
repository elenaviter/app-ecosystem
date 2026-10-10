"""W661, 10 Oct (live defect + operator binding): the Card file-lock root is the bundle's own storage root plus
_card_locks, never a host path; it works for a non-root process user and refuses by name when it cannot."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


def _entry(root):
    return SimpleNamespace(bundle_storage_root=lambda: root, config=SimpleNamespace(), bundle_props={})


def test_the_lock_root_is_the_bundle_storage_root_plus_card_locks(tmp_path):
    m = module()
    assert m._card_lock_root(_entry(tmp_path / "bundle")) == str(tmp_path / "bundle" / "_card_locks")
    assert m._card_lock_root(_entry(None)) is None


@pytest.mark.asyncio
async def test_without_a_storage_root_every_card_mutation_refuses_by_name(monkeypatch, tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    m = module()
    monkeypatch.setenv("GATEWAY_COMPONENT", "proc")
    lock = m._card_mutation_lock(_entry(None))
    with pytest.raises(CardStorageError, match="card_lock_root_unavailable"):
        async with lock(lock_path=tmp_path / "x" / ".lock", resource_id="delegated-card:a", operation="t",
                        wait_seconds=1):
            pass


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as the non-root process user (live chat-proc is uid 1000)")
@pytest.mark.asyncio
async def test_a_non_root_process_takes_the_lock_under_bundle_storage_and_an_unwritable_root_refuses(
        monkeypatch, tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    m = module()
    monkeypatch.setenv("GATEWAY_COMPONENT", "proc")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shared = bundle / "delegated-cards" / "v1" / "grantors" / ("s" * 64) / "cards" / "aut_a" / ".mutation.lock"
    lock = m._card_mutation_lock(_entry(bundle))
    async with lock(lock_path=shared, resource_id="delegated-card:aut_a", operation="t", wait_seconds=1):
        assert [p.suffix for p in (bundle / "_card_locks").iterdir()] == [".lock"]
    assert not shared.exists()

    locked = tmp_path / "read-only-bundle"
    locked.mkdir()
    locked.chmod(0o555)  # like /run for uid 1000: the root below it cannot be created
    try:
        lock = m._card_mutation_lock(_entry(locked))
        with pytest.raises(CardStorageError, match="card_lock_root_unwritable"):
            async with lock(lock_path=shared, resource_id="delegated-card:aut_a", operation="t", wait_seconds=1):
                pass
    finally:
        locked.chmod(0o755)


@pytest.mark.asyncio
async def test_a_configured_ttl_below_the_pb_bounds_refuses_by_name(monkeypatch, tmp_path):
    """Mint N4 / R-5: lock_ttl_seconds must stay above PB's 60 s idle-in-transaction bound."""
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    m = module()
    monkeypatch.setenv("GATEWAY_COMPONENT", "proc")
    monkeypatch.setattr(m, "_card_lock_settings", lambda entrypoint: {"lock_backend": "redis", "lock_ttl_seconds": 30})
    monkeypatch.setattr(m, "_card_lock_backend", lambda entrypoint: "redis")
    lock = m._card_mutation_lock(SimpleNamespace(bundle_storage_root=lambda: tmp_path, redis=object()))
    with pytest.raises(CardStorageError, match="card_lock_settings_invalid"):
        async with lock(lock_path=tmp_path / "x" / ".lock", resource_id="delegated-card:a", operation="t",
                        wait_seconds=1):
            pass

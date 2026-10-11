"""W661 K1: the Card mutation lock on a container-local path (option a)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.cards.locks import container_local_lock, container_local_lock_path


def test_one_shared_path_names_one_local_file_and_different_cards_differ(tmp_path):
    root = tmp_path / "locks"
    a = container_local_lock_path(root, "/bundle-storage/t/p/hub/delegated-cards/v1/grantors/x/cards/aut_a/.mutation.lock")
    assert a == container_local_lock_path(root, "/bundle-storage/t/p/hub/delegated-cards/v1/grantors/x/cards/aut_a/.mutation.lock")
    assert a.parent == root and a.name.endswith(".lock")
    assert a != container_local_lock_path(root, "/bundle-storage/t/p/hub/delegated-cards/v1/grantors/x/cards/aut_b/.mutation.lock")


@pytest.mark.asyncio
async def test_the_base_lock_is_taken_on_the_local_file_with_everything_else_unchanged(tmp_path):
    seen = []

    @asynccontextmanager
    async def base(*, lock_path, resource_id, operation, wait_seconds):
        seen.append((lock_path, resource_id, operation, wait_seconds))
        yield {"held": True}

    lock = container_local_lock(base, tmp_path / "locks")
    shared = tmp_path / "share" / "cards" / "aut_a" / ".mutation.lock"
    async with lock(lock_path=shared, resource_id="delegated-card:aut_a", operation="delegated-card-mutation",
                    wait_seconds=5) as held:
        assert held == {"held": True}
    assert seen == [(container_local_lock_path(tmp_path / "locks", shared), "delegated-card:aut_a",
                     "delegated-card-mutation", 5)]
    assert not shared.exists()  # nothing is written on the share


def test_a_relative_root_is_refused():
    with pytest.raises(ValueError, match="card_lock_root_not_absolute"):
        container_local_lock(lambda **kwargs: None, "relative/locks")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ingress", "", "openapi"])
async def test_outside_the_proc_role_the_card_lock_is_refused_before_it_is_taken(tmp_path, role):
    from connection_hub.delegated_credentials.cards.locks import proc_only_lock
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    taken = []

    @asynccontextmanager
    async def base(**kwargs):
        taken.append(kwargs["lock_path"])
        yield {}

    lock = proc_only_lock(base, role=lambda: role)
    with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
        async with lock(lock_path=tmp_path / "x.lock", resource_id="r", operation="o", wait_seconds=1):
            pass
    assert taken == []


@pytest.mark.asyncio
async def test_in_the_proc_role_the_lock_is_the_container_local_one(tmp_path):
    from connection_hub.delegated_credentials.cards.locks import hub_card_mutation_lock

    taken = []

    @asynccontextmanager
    async def base(**kwargs):
        taken.append(kwargs["lock_path"])
        yield {}

    lock = hub_card_mutation_lock(base, tmp_path / "locks", role=lambda: "proc")
    shared = tmp_path / "share" / ".mutation.lock"
    async with lock(lock_path=shared, resource_id="r", operation="o", wait_seconds=1):
        pass
    assert taken == [container_local_lock_path(tmp_path / "locks", shared)]


@pytest.mark.asyncio
async def test_a_card_save_in_an_ingress_process_is_refused_and_writes_nothing(tmp_path):
    """EMain 18:40Z: a Card mutation outside chat-proc refuses with a named code instead of racing."""
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.locks import hub_card_mutation_lock
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
    from test_card_service import _Cache
    from test_w661_card_versions import DIGEST, SUBJECT_HASH, TXN, WHEN, _setup

    store, _, before, after = await _setup(tmp_path)

    @asynccontextmanager
    async def base(**kwargs):
        yield {}

    # The ingress worker owns a distinct store object over the same files.
    ingress_store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(ingress_store, store._card_transaction_decisions)
    service = DelegatedCardService(store=ingress_store, cache=_Cache(),
                                   mutation_lock=hub_card_mutation_lock(base, tmp_path / "locks", role=lambda: "ingress"))
    with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
        await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                         members=[(SUBJECT_HASH, before.access_id, 1, after)])
    assert not tx.card_version_marker_path(store, TXN).exists()
    with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
        await service.commit(after, subject_hash=SUBJECT_HASH, expected_revision=1, now=1_780_000_100)
    assert (await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)).card_revision == 1


@pytest.mark.asyncio
async def test_the_store_level_guard_refuses_every_card_file_write_outside_proc_even_without_the_lock(tmp_path):
    """Infra 18:43Z: the boundary covers version, current.json and marker writes, not only the lock."""
    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards.locks import guard_card_store_writes
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
    from test_card_service import SUBJECT_HASH, _authority
    from datetime import datetime, timezone

    role = {"value": "ingress"}
    store = guard_card_store_writes(BundleStorageDelegatedCardStore(tmp_path / "share"), role=lambda: role["value"])
    try:
        card = _authority()
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            await store.write_revision(subject_hash=SUBJECT_HASH, authority=card, updated_at=datetime.now(timezone.utc))
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            await durable_io.write_json_atomic(store.root / "card-versions" / "x.json", {"a": 1})
        assert not (tmp_path / "share").exists() or not any((tmp_path / "share").rglob("*.json"))
        await durable_io.write_json_atomic(tmp_path / "elsewhere.json", {"a": 1})  # outside the Card root: unaffected
        role["value"] = "proc"
        pointer = await store.write_revision(subject_hash=SUBJECT_HASH, authority=card,
                                             updated_at=datetime.now(timezone.utc))
        assert pointer.card_revision == card.card_revision
    finally:
        durable_io._WRITE_GUARDS.clear()


def test_a_deletion_under_the_card_root_is_refused_outside_proc(tmp_path):
    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards.locks import guard_card_store_writes
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError

    store = BundleStorageDelegatedCardStore(tmp_path / "share")
    marker = store.root / "card-versions" / "t.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    role = {"value": "ingress"}
    guard_card_store_writes(store, role=lambda: role["value"])
    try:
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            durable_io.unlink_guarded(marker)
        assert marker.exists()
        role["value"] = "proc"
        durable_io.unlink_guarded(marker)
        assert not marker.exists()
    finally:
        durable_io._WRITE_GUARDS.clear()


@pytest.mark.asyncio
async def test_direct_stage_publish_and_rollback_without_the_service_lock_are_refused_outside_proc(tmp_path):
    """Infra 18:46Z: the marker write, the revision sidecar and the unlinks in transaction_store bypass
    store.write_revision; the store-level guard refuses them too, called directly with no Card lock at all."""
    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.locks import guard_card_store_writes
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    from test_w661_card_versions import DIGEST, SUBJECT_HASH, TXN, WHEN, _setup

    store, _, before, after = await _setup(tmp_path)  # seeded before the guard exists
    role = {"value": "ingress"}
    guard_card_store_writes(store, role=lambda: role["value"])
    try:
        files = sorted(p.relative_to(store.root) for p in store.root.rglob("*"))
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            await tx.card_version_stage(store, txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                        members=[(SUBJECT_HASH, before.access_id, 1, after)])
        assert sorted(p.relative_to(store.root) for p in store.root.rglob("*")) == files  # nothing written
        role["value"] = "proc"
        await tx.card_version_stage(store, txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                    members=[(SUBJECT_HASH, before.access_id, 1, after)])
        staged = sorted(p.relative_to(store.root) for p in store.root.rglob("*"))
        current = store.current_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).read_bytes()
        role["value"] = "ingress"
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            await tx.card_version_publish(store, txn=TXN)  # current.json is never moved
        assert store.current_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).read_bytes() == current
        assert sorted(p.relative_to(store.root) for p in store.root.rglob("*")) == staged
        with pytest.raises(CardStorageError, match="card_store_write_wrong_process_role"):
            await tx.card_version_rollback(store, txn=TXN)  # the first unlink refuses
        assert sorted(p.relative_to(store.root) for p in store.root.rglob("*")) == staged  # nothing removed
    finally:
        durable_io._WRITE_GUARDS.clear()

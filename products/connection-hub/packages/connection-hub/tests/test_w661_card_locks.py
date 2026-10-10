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

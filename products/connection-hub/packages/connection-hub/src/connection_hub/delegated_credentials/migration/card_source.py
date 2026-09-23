# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable Card-authority lookup used by handle migration."""

from __future__ import annotations

from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
)


class BundleStorageCardAuthorityLoader:
    """Resolve a stable Card id from the bundle-owned immutable store."""

    def __init__(self, store: BundleStorageDelegatedCardStore) -> None:
        if store is None:
            raise ValueError("BundleStorageCardAuthorityLoader requires a store")
        self._store = store

    async def load_card_authority(self, access_id: str) -> CardAuthority | None:
        return await self._store.find_current_authority(access_id)


__all__ = ["BundleStorageCardAuthorityLoader"]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.catalog.publisher import ensure_delegated_catalog
from connection_hub.delegated_credentials.catalog.store import (
    BundleStorageDelegatedCatalogStore,
)

CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "resources": [
                {
                    "resource": "https://example.test/mcp",
                    "grants": ["named_services:use"],
                }
            ],
        }
    }
}


class _Cache:
    def __init__(self) -> None:
        self.active = None

    async def cache_version(self, document, **kwargs) -> None:
        return None

    async def publish_active(self, document, **kwargs) -> bool:
        self.active = document
        return True

    async def read_active(self):
        return self.active


@pytest.mark.asyncio
async def test_publication_uses_host_shared_operation_runner(tmp_path):
    calls = []

    async def operation_runner(**kwargs):
        calls.append(kwargs)
        assert await kwargs["ready"]() is False
        await kwargs["action"]()

    store = BundleStorageDelegatedCatalogStore(tmp_path)
    result = await ensure_delegated_catalog(
        connections=CONNECTIONS,
        store=store,
        cache=_Cache(),
        operation_runner=operation_runner,
        reason="test",
    )

    assert result.created is True
    assert (await store.read_active()).version == result.version
    assert calls[0]["operation"] == "delegated-catalog-publish"
    assert calls[0]["storage_root"] == store.root

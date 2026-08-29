# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

from pathlib import Path

import pytest

from prokura.delegated_credentials.serving import (
    DelegatedServingResolvers,
    build_delegated_serving_resolvers,
)


@pytest.mark.asyncio
async def test_serving_readers_require_a_host_storage_port(tmp_path: Path):
    result = await build_delegated_serving_resolvers(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        bundle_id="prokura-app@1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_serving_readers_use_host_app_storage_and_props_ports(tmp_path: Path):
    storage_calls = []
    props_calls = []

    def storage_root_resolver(**kwargs):
        storage_calls.append(kwargs)
        return tmp_path

    async def bundle_props_loader(**kwargs):
        props_calls.append(kwargs)
        return {
            "connections": {
                "delegated_credentials": {
                    "cache": {"catalog_active_seconds": 41}
                }
            }
        }

    result = await build_delegated_serving_resolvers(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        app_id_resolver=lambda: "prokura-app@1",
        storage_root_resolver=storage_root_resolver,
        bundle_props_loader=bundle_props_loader,
    )

    assert isinstance(result, DelegatedServingResolvers)
    assert storage_calls == [
        {
            "bundle_id": "prokura-app@1",
            "tenant": "tenant-a",
            "project": "project-a",
            "ensure": False,
        }
    ]
    assert props_calls == [
        {
            "tenant": "tenant-a",
            "project": "project-a",
            "bundle_id": "prokura-app@1",
        }
    ]

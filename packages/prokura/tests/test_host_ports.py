# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import pytest

from prokura.authority_registry_client import AuthorityRegistryClient
from prokura.connection_edges import ConnectionEdgesClient


class _Entrypoint:
    bundle_props = {"connections": {"connection_hub": {"bundle_id": "hub@test"}}}

    def bundle_prop(self, path: str, default=None):
        current = self.bundle_props
        for part in path.split("."):
            if not isinstance(current, dict):
                return default
            current = current.get(part)
        return default if current is None else current

    def runtime_identity(self):
        return {"tenant": "tenant-a", "project": "project-a"}


def _registry():
    return {
        "authorities": {
            "platform": {
                "platform": True,
                "providers": {
                    "session": {
                        "type": "bundle_session_login",
                        "entrypoints": {
                            "login": {
                                "bundle_id": "workspace@1",
                                "route": "public",
                                "operation": "platform_login",
                            }
                        },
                    }
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_connection_edges_uses_injected_application_operation_port():
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {kwargs["operation"]: {"ok": True, "subject": "user-1"}}

    result = await ConnectionEdgesClient(
        _Entrypoint(), operation_caller=caller
    ).resolve_identity(provider="telegram", provider_subject="tg-1")

    assert result == {"ok": True, "subject": "user-1"}
    assert calls[0]["bundle_id"] == "hub@test"
    assert calls[0]["route"] == "operations"


@pytest.mark.asyncio
async def test_authority_registry_uses_injected_bundle_props_port():
    calls = []

    async def loader(redis, **kwargs):
        calls.append((redis, kwargs))
        return {"authority_registry": _registry()}

    result = await AuthorityRegistryClient(
        _Entrypoint(),
        redis="projection-store",
        bundle_props_loader=loader,
    ).resolve_provider(authority_id="platform", provider_id="session")

    assert result["ok"] is True
    assert calls == [
        (
            "projection-store",
            {"tenant": "tenant-a", "project": "project-a", "bundle_id": "hub@test"},
        )
    ]


@pytest.mark.asyncio
async def test_redis_backed_registry_requires_an_explicit_host_port():
    with pytest.raises(RuntimeError, match="bundle-props loader"):
        await AuthorityRegistryClient(_Entrypoint(), redis="projection-store").registry()

"""W880: a failed request-authenticator schema setup fails the Hub load visibly.

Before W880, ``on_bundle_load`` ran the authenticator ``ensure_schema`` and the
descriptor bootstrap inside one ``try`` that only logged. A Hub that lost a
concurrent-load race on the schema therefore served without bootstrapping its
request authenticators, and nothing but a log line said so (review of #828).
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


@pytest.fixture
def module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


class _Store:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def ensure_schema(self) -> None:
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_a_failed_schema_setup_fails_the_load_and_skips_nothing_silently(module, monkeypatch) -> None:
    bootstraps = []

    async def bootstrap(entrypoint):
        bootstraps.append(entrypoint)
        return 1

    monkeypatch.setattr(module, "_authenticator_store", lambda entrypoint: _Store(RuntimeError("deadlock detected")))
    monkeypatch.setattr(module, "_bootstrap_descriptor_authenticators", bootstrap)

    with pytest.raises(RuntimeError, match="deadlock detected"):
        await module._ensure_request_authenticators(SimpleNamespace())
    assert bootstraps == []


@pytest.mark.asyncio
async def test_a_failed_descriptor_bootstrap_is_logged_as_before(module, monkeypatch, caplog) -> None:
    async def bootstrap(entrypoint):
        raise ValueError("bad descriptor authenticator")

    monkeypatch.setattr(module, "_authenticator_store", lambda entrypoint: _Store())
    monkeypatch.setattr(module, "_bootstrap_descriptor_authenticators", bootstrap)
    caplog.set_level(logging.ERROR)

    assert await module._ensure_request_authenticators(SimpleNamespace()) == 0
    assert any("failed to bootstrap request authenticators" in record.getMessage() for record in caplog.records)


def test_on_bundle_load_does_not_swallow_the_schema_setup(module) -> None:
    source = inspect.getsource(module.ConnectionHubEntrypoint.on_bundle_load)
    tree = ast.parse(source.strip())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and "_ensure_request_authenticators" in ast.unparse(node.func)]
    assert calls, "on_bundle_load must ensure the request authenticators"
    swallowing = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
                  and any("_ensure_request_authenticators" in ast.unparse(statement) for statement in node.body)]
    assert swallowing == [], "a failed schema setup must fail the load, not be caught in on_bundle_load"

"""The account-connect callback consumes its opaque state once and hands it to the completion."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

from connection_hub.delegated_to_kdcube.oauth import MemoryOAuthStateStore, create_oauth_state

SECRET = "s" * 32


@pytest.fixture
def callback(monkeypatch):
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    store = MemoryOAuthStateStore()
    completions = []

    class _Operations:
        async def complete_oauth(self, **kwargs):
            completions.append(kwargs)
            return {"account": {"label": "Work account"}}

    async def _secret(*_args, **_kwargs):
        return SECRET

    monkeypatch.setattr(module, "_delegated_to_kdcube_oauth_state_store", lambda *_args: store)
    monkeypatch.setattr(module, "_delegated_to_kdcube_oauth_state_secret", _secret)
    monkeypatch.setattr(module, "_delegated_to_kdcube_operations", lambda *_args: _Operations())
    monkeypatch.setattr(module, "_delegated_to_kdcube_oauth_callback_url",
                        lambda *_args: "https://hub.example.test/callback")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    request = SimpleNamespace(headers={"host": "hub.example.test", "x-forwarded-proto": "https"})

    async def call(state):
        return await module.ConnectionHubEntrypoint.delegated_to_kdcube_oauth_callback(
            instance, request=request, code="provider-code", state=state)

    return SimpleNamespace(call=call, store=store, completions=completions)


@pytest.mark.asyncio
async def test_the_callback_consumes_the_state_once_and_passes_what_it_consumed(callback):
    state = await create_oauth_state(callback.store, secret=SECRET, user_id="user-1", provider_id="google",
                                     connector_app_id="kdcube_gmail", claims=("gmail:read",))
    state = state["state"]
    await callback.call(state)
    [completion] = callback.completions
    assert completion["state"] == state and completion["consumed_state"]["user_id"] == "user-1"
    # A second callback with the same state finds it used and completes nothing.
    await callback.call(state)
    assert len(callback.completions) == 1

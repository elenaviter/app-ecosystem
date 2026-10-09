"""W676: the Hub binds Card transactions with its OAuth issuance authority and no Card handle store.

Without the issuance authority an original OAuth code exchange refused card_transactions_unavailable (W603).
A Card edit never moves or depends on a credential's handle row (operator, 2026-10-09: "changing something on
teh card does not change the credential"), so no handle store is bound into Card transactions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def _entrypoint_module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


@pytest.mark.asyncio
async def test_card_transactions_bind_the_issuance_authority_and_no_handle_store(monkeypatch):
    module = _entrypoint_module()
    handles, issuance, decisions = object(), object(), object()
    captured = {}

    async def decision_store(entrypoint, pool):
        return decisions

    async def callers(entrypoint, persistence):
        return SimpleNamespace(authorities={})

    async def forwarders(entrypoint):
        return {}

    def bind(service, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(module, "_delegated_authority_config", lambda entrypoint: SimpleNamespace(uses_postgresql=True))
    monkeypatch.setattr(module, "_card_decision_store", decision_store)
    monkeypatch.setattr(module, "_card_participant_callers", callers)
    monkeypatch.setattr(module, "_managed_card_edit_forwarders", forwarders)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda entrypoint: None)
    monkeypatch.setattr(module, "_delegated_catalog_store", lambda entrypoint: None)
    monkeypatch.setattr(module, "_connections_config", lambda entrypoint: {})
    monkeypatch.setattr(module, "_durable_authority", lambda entrypoint: SimpleNamespace(oauth=issuance))
    monkeypatch.setattr(module, "bind_card_transactions", bind)

    service = SimpleNamespace(bind_managed_card_edit=lambda forwarders: None)
    persistence = SimpleNamespace(credential_handles=handles)
    await module._bind_card_transactions(SimpleNamespace(pg_pool=object()), service,
                                         persistence=persistence, grant_store=None)
    assert captured.get("credential_handles") is None
    assert captured["issuance_store"] is issuance
    assert captured["decisions"] is decisions

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _load_entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _module_name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


def test_kdcube_runtime_ports_wrap_portable_connection_hub_policy():
    module = _load_entrypoint_module()

    assert module.AutomationAccessService.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.connection_hub."
    )
    assert module.DurableCardPersistence.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.connection_hub."
    )
    assert module.ensure_delegated_catalog.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.connection_hub."
    )
    assert module.DelegatedToKdcubeStore.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.connection_hub."
    )


def test_portable_authority_types_are_imported_from_connection_hub():
    module = _load_entrypoint_module()

    assert module.ConnectionEdgeStore.__module__ == "connection_hub.hub.edges"
    assert module.AuthenticatorStore.__module__ == "connection_hub.hub.authenticator_store"
    assert module.DelegatedCatalogResolver.__module__.startswith(
        "connection_hub.delegated_credentials.catalog."
    )


@pytest.mark.asyncio
async def test_automation_access_composes_the_real_kdcube_postgresql_adapters(
    monkeypatch,
    tmp_path,
):
    module = _load_entrypoint_module()
    grant_store = object()
    catalog_resolver = object()
    invocation_policies = object()

    class _DurableAuthority:
        def __init__(self) -> None:
            self.card_handles = object()
            self.ready = False

        async def ensure_ready(self) -> None:
            self.ready = True

    durable = _DurableAuthority()
    authority_config = SimpleNamespace(
        backend="postgresql",
        uses_postgresql=True,
    )

    async def _grant_store(_entrypoint):
        return grant_store

    monkeypatch.setattr(module, "_oauth_grant_store", _grant_store)
    monkeypatch.setattr(
        module,
        "_delegated_authority_config",
        lambda _entrypoint: authority_config,
    )
    monkeypatch.setattr(
        module,
        "_durable_authority",
        lambda _entrypoint: durable,
    )
    monkeypatch.setattr(
        module,
        "_runtime_tenant_project",
        lambda _entrypoint: ("tenant-a", "project-a"),
    )
    monkeypatch.setattr(
        module,
        "_connections_config",
        lambda _entrypoint: {},
    )
    monkeypatch.setattr(
        module,
        "_delegated_catalog_resolver",
        lambda _entrypoint, _redis: catalog_resolver,
    )
    monkeypatch.setattr(
        module,
        "_invocation_policy_service",
        lambda _entrypoint: invocation_policies,
    )
    entrypoint = SimpleNamespace(
        redis=object(),
        bundle_storage_root=lambda: tmp_path,
    )

    service = await module._automation_access_service_for(
        entrypoint,
        config=object(),
    )

    assert isinstance(service, module.AutomationAccessService)
    assert isinstance(service._persistence, module.DurableCardPersistence)
    assert service._store is grant_store
    assert service._catalog_resolver is catalog_resolver
    assert service._invocation_policies is invocation_policies
    assert service._persistence._handles is durable.card_handles
    assert durable.ready is True

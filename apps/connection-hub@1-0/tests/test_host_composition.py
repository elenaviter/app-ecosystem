from __future__ import annotations

from pathlib import Path

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _load_entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _module_name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


def test_kdcube_runtime_ports_wrap_portable_prokura_policy():
    module = _load_entrypoint_module()

    assert module.AutomationAccessService.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.prokura."
    )
    assert module.DurableCardPersistence.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.prokura."
    )
    assert module.ensure_delegated_catalog.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.prokura."
    )
    assert module.DelegatedToKdcubeStore.__module__.startswith(
        "kdcube_ai_app.apps.chat.sdk.integrations.prokura."
    )


def test_portable_authority_types_are_imported_from_prokura():
    module = _load_entrypoint_module()

    assert module.ConnectionEdgeStore.__module__ == "prokura.hub.edges"
    assert module.AuthenticatorStore.__module__ == "prokura.hub.authenticator_store"
    assert module.DelegatedCatalogResolver.__module__.startswith(
        "prokura.delegated_credentials.catalog."
    )

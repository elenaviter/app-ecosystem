"""Direct hosting-handler regressions; NOT actual mounted authentication proof."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.issuer_gate import change_digest
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def _module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


def _body():
    targets = [{"owner_subject": f"owner-{i}", "access_id": f"card-{i}", "expected_card_revision": 1,
        "expected_authority_fingerprint": "a" * 64, "issuer_kind": f"opaque-{i}", "issuer_ref": "opaque"}
        for i in range(2)]
    return {"context_ref": "reserved-context", "request_id": "recorded-request", "action": "revoke",
        "change_digest": change_digest({"action": "revoke", "targets": targets}), "targets": targets}


def _entry(module, *, user_type="registered", user_id="actual-human", username="human", authority=None):
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.comm_context = SimpleNamespace(user=SimpleNamespace(user_type=user_type, user_id=user_id,
        username=username, identity_authority=authority or {}))
    return instance


@pytest.mark.asyncio
async def test_separate_sdk_metadata_never_selects_lifecycle_actor(monkeypatch):
    module = _module()
    service = MagicMock()
    service.issuer_managed_lifecycle_apply = AsyncMock(return_value={"ok": True})
    factory = AsyncMock(return_value=service)
    monkeypatch.setattr(module, "_automation_access_service", factory)
    instance = _entry(module)
    result = await module.ConnectionHubEntrypoint.issuer_managed_lifecycle_apply(instance,
        user_id="forged-other-human", fingerprint="forged", **_body())
    assert result == {"ok": True}
    service.bind_issuer_registry.assert_called_once_with(service._issuers, actor_subject="actual-human")
    service.issuer_managed_lifecycle_apply.assert_awaited_once_with(_body())


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["actor_subject", "owner_subject", "user_id", "fingerprint", "approval_hash"])
async def test_nested_dto_extra_fields_refuse_before_service_or_any_card_effect(monkeypatch, extra):
    module = _module()
    factory = AsyncMock()
    monkeypatch.setattr(module, "_automation_access_service", factory)
    body = _body()
    body[extra] = "forged"
    result = await module.ConnectionHubEntrypoint.issuer_managed_lifecycle_apply(_entry(module), data=body)
    assert result["ok"] is False and result["status"] == 400
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"user_type": "external", "user_id": "owner-0", "username": "integration:agent:owner-0",
        "authority": {"authority_id": "delegated_client", "grantor_user_id": "owner-0"}},
    {"user_type": "registered", "authority": {"delegated_card_binding": {"access_id": "agent"}}},
    {"user_type": "anonymous"},
])
async def test_non_human_context_cannot_borrow_owner_or_metadata(monkeypatch, changes):
    module = _module()
    factory = AsyncMock()
    monkeypatch.setattr(module, "_automation_access_service", factory)
    result = await module.ConnectionHubEntrypoint.issuer_managed_lifecycle_apply(_entry(module, **changes),
        user_id="actual-human", **_body())
    assert result == {"ok": False, "error": "issuer_lifecycle_requires_platform_human", "status": 403}
    factory.assert_not_called()


def test_alias_and_csrf_are_explicit_and_unconfigured_flock_capability_refuses(monkeypatch):
    module = _module()
    method = module.ConnectionHubEntrypoint.issuer_managed_lifecycle_apply.__bundle_api_method__
    assert method.alias == "issuer_managed_lifecycle_apply" and method.http_method == "POST"
    assert method.route == "operations"
    assert method.alias in module.CSRF_PROTECTED_OPERATION_ALIASES
    for raw in ({}, {"delegated_credentials": "invalid"}, {"delegated_credentials": {"lifecycle_storage": {"lock_scope": "shared-without-proof"}}}):
        monkeypatch.setattr(module, "_connections_config", lambda entrypoint: raw)
        assert module._lifecycle_lock_scope(object()) == ""

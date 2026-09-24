"""Project-person Control Card operations keep host authority out of payloads."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def project_person_control_get(self, user, **kwargs):
        self.calls.append(("get", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_create(self, user, **kwargs):
        self.calls.append(("create", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_update(self, user, **kwargs):
        self.calls.append(("update", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_revoke(self, user, **kwargs):
        self.calls.append(("revoke", {"user": user, **kwargs}))
        return {"ok": True}


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _Service()

    async def _access_service(*_args, **_kwargs):
        return service

    monkeypatch.setattr(module, "_automation_access_service", _access_service)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"user_id": "authenticated-admin"},
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


def test_operations_are_declared_as_csrf_protected_posts() -> None:
    module = _entrypoint_module()
    methods = {
        alias: getattr(
            getattr(module.ConnectionHubEntrypoint, alias),
            "__bundle_api_method__",
        )
        for alias in (
            "project_person_control_get",
            "project_person_control_create",
            "project_person_control_update",
            "project_person_control_revoke",
        )
    }

    assert all(method.alias == alias for alias, method in methods.items())
    assert all(method.http_method == "POST" for method in methods.values())
    assert set(methods).issubset(module.CSRF_PROTECTED_OPERATION_ALIASES)


@pytest.mark.asyncio
async def test_operations_use_authenticated_actor_and_host_request_id(entrypoint) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(request_id="host-request-7"),
        scope={},
    )
    forged = {
        "project_ref": "work:project:quickstart",
        "target_subject": "platform-user-2",
        "actor_subject": "forged-admin",
        "request_id": "forged-request",
    }

    await entrypoint.module.ConnectionHubEntrypoint.project_person_control_get(
        entrypoint.instance,
        data=forged,
        request=request,
    )
    await entrypoint.module.ConnectionHubEntrypoint.project_person_control_create(
        entrypoint.instance,
        data={**forged, "label": "Quickstart member"},
        request=request,
    )
    await entrypoint.module.ConnectionHubEntrypoint.project_person_control_update(
        entrypoint.instance,
        data={
            **forged,
            "label": "Narrowed member",
            "expected_card_revision": 1,
        },
        request=request,
    )
    await entrypoint.module.ConnectionHubEntrypoint.project_person_control_revoke(
        entrypoint.instance,
        data=forged,
        request=request,
    )

    assert [call[0] for call in entrypoint.service.calls] == [
        "get",
        "create",
        "update",
        "revoke",
    ]
    for _operation, call in entrypoint.service.calls:
        assert call["user"] == {"user_id": "authenticated-admin"}
        assert call["project_ref"] == "work:project:quickstart"
        assert call["target_subject"] == "platform-user-2"
        assert call["request_id"] == "host-request-7"
        assert "actor_subject" not in call


@pytest.mark.asyncio
async def test_project_authorization_port_accepts_async_host_factory() -> None:
    module = _entrypoint_module()
    port = object()

    async def _factory():
        return port

    entrypoint = SimpleNamespace(project_authorization_port_factory=_factory)

    assert await module._project_authorization_port(entrypoint) is port


@pytest.mark.asyncio
async def test_raising_port_factory_isolated_to_project_person_operations(
    monkeypatch,
) -> None:
    module = _entrypoint_module()

    class _EmptyCards:
        async def load_current(self, access_id, *, subject_hash):
            del access_id, subject_hash
            return None

    async def _factory():
        raise RuntimeError("policy host unavailable")

    async def _card_persistence(*_args):
        return _EmptyCards()

    async def _grant_store(_entrypoint):
        return object()

    monkeypatch.setattr(
        module,
        "_runtime_tenant_project",
        lambda _entrypoint: ("demo-tenant", "demo-project"),
    )
    monkeypatch.setattr(
        module,
        "_delegated_authority_config",
        lambda _entrypoint: SimpleNamespace(backend="redis-migration-source"),
    )
    monkeypatch.setattr(module, "_delegated_catalog_resolver", lambda *_args: object())
    monkeypatch.setattr(module, "_delegated_card_persistence", _card_persistence)
    monkeypatch.setattr(module, "_oauth_grant_store", _grant_store)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda _entrypoint: None)

    service = await module._automation_access_service_for(
        SimpleNamespace(
            redis=object(),
            project_authorization_port_factory=_factory,
        ),
        SimpleNamespace(),
    )

    assert await service.revoke_access(
        {"user_id": "card-owner"},
        access_id="missing-card",
    ) == {"ok": True, "removed": False}
    assert await service.project_person_control_get(
        {"user_id": "project-admin"},
        project_ref="work:project:quickstart",
        target_subject="platform-user-2",
        request_id="request-port-failure",
    ) == {
        "ok": False,
        "error": "project_person_control_authorization_unavailable",
        "reason": "authorization_port_not_configured",
        "retryable": True,
        "status": 503,
    }

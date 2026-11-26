"""An application's synthetic holder never authenticates as a person.

The shared descriptor Control lives under ``application-authority:<digest>``.
Only the Hub acts for that holder, after its own checks (the deployment's
publication, a platform administrator's Save). A request whose user id is
that subject is treated as unauthenticated at the Hub boundary.
"""

from __future__ import annotations

from pathlib import Path

from connection_hub.delegated_credentials.controls.descriptor_control import application_authority_subject
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def _entrypoint_module():
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    return module


def test_the_application_holder_is_never_a_request_user(monkeypatch) -> None:
    module = _entrypoint_module()
    holder = application_authority_subject(tenant="tenant-a", project="project-a", application="app@1-0")
    monkeypatch.setattr(module, "_platform_user_id", lambda *_a, **_k: holder)
    monkeypatch.setattr(module, "_entrypoint_user_roles_permissions", lambda *_a: (["kdcube:role:super-admin"], []))

    assert module._platform_user_payload(object()) == {}


def test_an_ordinary_user_still_authenticates(monkeypatch) -> None:
    module = _entrypoint_module()
    monkeypatch.setattr(module, "_platform_user_id", lambda *_a, **_k: "user-1")
    monkeypatch.setattr(module, "_entrypoint_user_roles_permissions", lambda *_a: ([], []))
    monkeypatch.setattr(module, "resolve_principal_roles", lambda **_k: {})
    monkeypatch.setattr(module, "_identity_config", lambda *_a: {})

    assert module._platform_user_payload(object())["user_id"] == "user-1"


def _migrate_call(module, monkeypatch, roles, payload):
    import asyncio

    calls = []

    async def migrate(host, store, *, apply):
        calls.append(apply)
        return {"ok": True, "applied": apply}

    async def service(*_a, **_k):
        return object()

    import connection_hub.delegated_credentials.descriptor_control_migration as migration
    monkeypatch.setattr(migration, "migrate_descriptor_controls", migrate)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *_a, **_k: {"user_id": "user-1", "roles": roles})
    monkeypatch.setattr(module, "_automation_access_service", service)
    monkeypatch.setattr(module, "_guarded_card_store", lambda *_a, **_k: object())
    monkeypatch.setattr(module, "_lifecycle_lock_scope", lambda *_a: "scope")
    import connection_hub.delegated_credentials.cards.locks as locks
    monkeypatch.setattr(locks, "current_process_role", lambda: locks.CARD_WRITER_ROLE)
    entry = object.__new__(module.ConnectionHubEntrypoint) if hasattr(module, "ConnectionHubEntrypoint") else None
    target = entry if entry is not None else next(
        value for value in vars(module).values()
        if isinstance(value, type) and hasattr(value, "descriptor_controls_migrate")).__new__(next(
        value for value in vars(module).values()
        if isinstance(value, type) and hasattr(value, "descriptor_controls_migrate")))
    target.bundle_storage_root = lambda: "/tmp/cards"
    method = type(target).descriptor_controls_migrate
    method = getattr(method, "__wrapped__", method)
    answer = asyncio.run(method(target, data=payload))
    return answer, calls


def test_the_migration_is_for_a_platform_administrator_and_dry_runs_by_default(monkeypatch) -> None:
    module = _entrypoint_module()
    refused, calls = _migrate_call(module, monkeypatch, ["kdcube:role:registered"], {"apply": True})
    assert refused == {"ok": False, "error": "platform_admin_required", "status": 403} and calls == []

    dry, calls = _migrate_call(module, monkeypatch, ["kdcube:role:super-admin"], {})
    assert dry == {"ok": True, "applied": False} and calls == [False]

    applied, calls = _migrate_call(module, monkeypatch, ["kdcube:role:super-admin"], {"apply": True})
    assert applied == {"ok": True, "applied": True} and calls == [True]

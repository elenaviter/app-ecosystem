"""W41 F6: an application's synthetic holder never authenticates as a person.

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

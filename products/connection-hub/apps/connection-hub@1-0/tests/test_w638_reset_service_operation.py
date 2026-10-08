"""W638: delegated_access_reset_service dispatches preview and confirm to the owner's service, exactly."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


class Recording:
    def __init__(self):
        self.calls = []

    async def reset_service_preview(self, user, **kwargs):
        self.calls.append(("preview", user, kwargs))
        return {"ok": True}

    async def reset_service_confirm(self, user, **kwargs):
        self.calls.append(("confirm", user, kwargs))
        return {"ok": True}


@pytest.fixture()
def entrypoint(monkeypatch):
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    service = Recording()

    async def access_service(*_args, **_kwargs):
        return service

    monkeypatch.setattr(module, "_automation_access_service", access_service)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "google:1"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


def call(entrypoint, data):
    import asyncio
    return asyncio.run(entrypoint.module.ConnectionHubEntrypoint.delegated_access_reset_service(
        entrypoint.instance, data=data))


def test_preview_and_confirm_reach_the_service_with_exactly_their_fields(entrypoint):
    assert call(entrypoint, {"phase": "preview", "access_id": " aut_1 ", "resource": "svc"}) == {"ok": True}
    assert call(entrypoint, {"phase": "confirm", "access_id": "aut_1", "resource": "svc", "original_revision": 3,
                             "display_digest": "a" * 64, "request_id": " reset-1 "}) == {"ok": True}
    assert entrypoint.service.calls == [
        ("preview", {"user_id": "google:1"}, {"access_id": "aut_1", "resource": "svc"}),
        ("confirm", {"user_id": "google:1"}, {"access_id": "aut_1", "resource": "svc", "original_revision": 3,
                                              "display_digest": "a" * 64, "request_id": "reset-1"}),
    ]


@pytest.mark.parametrize("data", [
    {"phase": "commit", "access_id": "a", "resource": "s"},
    {"phase": "confirm", "access_id": "a", "resource": "s", "original_revision": "3", "display_digest": "d",
     "request_id": "r"},
    {"phase": "confirm", "access_id": "a", "resource": "s", "original_revision": 3, "display_digest": "d"},
])
def test_an_unknown_phase_or_incomplete_confirm_reaches_nothing(entrypoint, data):
    assert call(entrypoint, data)["error"] == "card_reset_request_invalid"
    assert entrypoint.service.calls == []


def test_an_unauthenticated_request_reaches_nothing(entrypoint, monkeypatch):
    monkeypatch.setattr(entrypoint.module, "_platform_user_payload", lambda *a, **kw: None)
    assert call(entrypoint, {"phase": "preview", "access_id": "a", "resource": "s"})["error"] == \
        "delegated_access_requires_authenticated_user"
    assert entrypoint.service.calls == []

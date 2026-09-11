from __future__ import annotations

import json
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


class _Redis:
    def __init__(self, subscribers: int = 1) -> None:
        self.subscribers = subscribers
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return self.subscribers


class _Edit:
    def __init__(self, *, scope: str, changed: tuple[str, ...]) -> None:
        self.path = "/config/bundles.yaml"
        self.backup = "/config/bundles.yaml.bak-stamp"
        self.scope = scope
        self.changed = changed
        self.activation = "refresh" if scope == "lane" else "publish"

    def to_dict(self):
        return {
            "path": self.path,
            "backup": self.backup,
            "scope": self.scope,
            "changed": list(self.changed),
            "activation": self.activation,
            "problems": [],
        }


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = _Redis()
    monkeypatch.setattr(module, "_platform_admin_denied", lambda *_args: None)
    monkeypatch.setattr(module, "_runtime_tenant_project", lambda *_args: ("tenant-a", "project-a"))
    monkeypatch.setattr(module, "_entrypoint_bundle_id", lambda *_args: "connection-hub@1-0")
    monkeypatch.setattr(module, "_platform_user_id", lambda *_args, **_kwargs: "admin-1")

    async def describe(_entrypoint):
        return {"ok": True, "platform": {}, "authorities": []}

    monkeypatch.setattr(module, "_describe_authorities", describe)
    return SimpleNamespace(module=module, instance=instance)


def test_authority_registry_rereads_the_staged_descriptor(entrypoint, monkeypatch):
    from kdcube_ai_app.apps.chat.sdk import config_scopes

    entrypoint.instance.bundle_props = {
        "authority_registry": {"authorities": {"stale": {}}},
    }
    paths: list[str] = []

    def load(path: str):
        paths.append(path)
        return {"authorities": {"current": {}}}

    monkeypatch.setattr(config_scopes, "_load_bundles_plain", load)

    registry = entrypoint.module._authority_registry_config(entrypoint.instance)

    assert paths == ["connection-hub@1-0.authority_registry"]
    assert list(registry["authorities"]) == ["current"]


def test_app_defined_sign_in_options_keep_the_bundle_lookup(entrypoint):
    assert entrypoint.module._AUTH_TYPE_FOR_PROVIDER_TYPE["bundle"] == "bundle"
    assert entrypoint.module._AUTH_TYPE_FOR_PROVIDER_TYPE["cognito"] == "bundle"
    assert entrypoint.module._AUTH_TYPE_FOR_PROVIDER_TYPE["simple"] == "bundle"


@pytest.mark.asyncio
async def test_provider_edit_refuses_non_admin_before_write_or_publish(entrypoint, monkeypatch):
    monkeypatch.setattr(
        entrypoint.module,
        "_platform_admin_denied",
        lambda *_args: {"ok": False, "error": "platform_admin_required"},
    )

    result = await entrypoint.module.ConnectionHubEntrypoint.authority_provider_set(
        entrypoint.instance,
        data={"yaml": "this is not parsed"},
    )

    assert result == {
        "ok": False,
        "error": "platform_admin_required",
        "message": "The sign-in authorities are available to platform administrators only.",
    }
    assert entrypoint.instance.redis.published == []


@pytest.mark.asyncio
async def test_provider_edit_publishes_value_free_update_and_reports_live(entrypoint, monkeypatch):
    from kdcube_ai_app.infra.descriptors import edit as descriptor_edit

    changed = ("connection-hub@1-0.authority_registry.authorities.kdcube.platform.providers.cognito",)
    monkeypatch.setattr(
        descriptor_edit,
        "edit_bundle_authority_provider",
        lambda **_kwargs: _Edit(scope="providers", changed=changed),
    )

    result = await entrypoint.module.ConnectionHubEntrypoint.authority_provider_set(
        entrypoint.instance,
        data={
            "authority_id": "kdcube.platform",
            "provider_id": "cognito",
            "yaml": "type: simple_idp\nenabled: true\n",
        },
    )

    assert result["activation"] == "live"
    assert result["publication"]["subscribers"] == 1
    channel, raw = entrypoint.instance.redis.published[0]
    payload = json.loads(raw)
    assert channel == "kdcube:config:platform-settings:update:tenant-a:project-a"
    assert payload["section"] == "auth" and payload["scope"] == "providers"
    assert payload["actor"] == "admin-1"
    assert payload["changed"] == list(changed)
    assert "provider" not in payload and "value" not in payload


@pytest.mark.asyncio
async def test_provider_edit_requires_refresh_when_no_listener_received_update(entrypoint, monkeypatch):
    from kdcube_ai_app.infra.descriptors import edit as descriptor_edit

    entrypoint.instance.redis.subscribers = 0
    monkeypatch.setattr(
        descriptor_edit,
        "edit_bundle_authority_provider",
        lambda **_kwargs: _Edit(scope="providers", changed=("provider",)),
    )

    result = await entrypoint.module.ConnectionHubEntrypoint.authority_provider_set(
        entrypoint.instance,
        data={
            "authority_id": "kdcube.platform",
            "provider_id": "cognito",
            "yaml": "type: simple_idp\n",
        },
    )

    assert result["activation"] == "refresh"
    assert result["publication"] == {
        "ok": True,
        "event_id": result["publication"]["event_id"],
        "subscribers": 0,
    }


@pytest.mark.asyncio
async def test_lane_edit_is_published_but_remains_refresh_only(entrypoint, monkeypatch):
    from kdcube_ai_app.infra.descriptors import edit as descriptor_edit

    monkeypatch.setattr(
        descriptor_edit,
        "edit_assembly_platform_sign_in",
        lambda **_kwargs: _Edit(scope="lane", changed=("auth.type", "auth.connection_hub.provider_id")),
    )

    result = await entrypoint.module.ConnectionHubEntrypoint.platform_sign_in_set(
        entrypoint.instance,
        data={"provider_id": "server_login"},
    )

    assert result["activation"] == "refresh"
    assert result["publication"]["subscribers"] == 1
    payload = json.loads(entrypoint.instance.redis.published[0][1])
    assert payload["scope"] == "lane"

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


class _Access:
    def __init__(self):
        self.create_calls = []
        self.revoke_calls = []

    async def list_access(self, _user):
        return {
            "ok": True,
            "platform_user_id": "user-1",
            "items": [
                {
                    "access_id": "access-1",
                    "resource_operations": {
                        "urn:example:service": ["records.read", "records.delete"]
                    },
                    "account_scope": {
                        "slack": {"account-1": ["files:write"]}
                    },
                }
            ],
            "resources": [],
            "grant_options": [],
        }

    async def create_access(self, user, **kwargs):
        self.create_calls.append((user, kwargs))
        return {
            "ok": True,
            "access": {
                "access_id": "aut-secret-1",
                "resource_operations": kwargs.get("resource_operations") or {},
            },
            "access_token": "not-returned-until-policies-exist",
        }

    async def revoke_access(self, user, *, access_id):
        self.revoke_calls.append((user, access_id))
        return {"ok": True, "removed": True}


class _Policy:
    def __init__(self, authority, mode="once", revision=1):
        self.authority = authority
        self.mode = mode
        self.revision = revision

    def to_public_dict(self):
        return {
            "policy_id": "invpol-demo",
            "authority": self.authority.to_dict(),
            "mode": self.mode,
            "revision": self.revision,
            "state": "available",
            "remaining": 1 if self.mode == "once" else None,
        }


class _Policies:
    def __init__(self):
        self.set_calls = []
        self.policy = None

    async def set_policy(self, **kwargs):
        self.set_calls.append(kwargs)
        self.policy = _Policy(kwargs["authority"], kwargs["mode"])
        return self.policy

    async def list_for_card(self, **_kwargs):
        return [self.policy] if self.policy is not None else []


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    access = _Access()
    policies = _Policies()
    monkeypatch.setattr(module, "_platform_user_payload", lambda *_args, **_kwargs: {"sub": "user-1"})
    monkeypatch.setattr(module, "_automation_access_service", lambda *_args: access)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda *_args: policies)
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(
        module=module,
        instance=instance,
        access=access,
        policies=policies,
    )


@pytest.mark.asyncio
async def test_card_operation_policy_is_owner_scoped_and_returned_in_listing(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.delegated_invocation_policy_set(
        entrypoint.instance,
        data={
            "access_id": "access-1",
            "resource": "urn:example:service",
            "operation": "records.delete",
            "mode": "once",
            "expected_revision": 0,
        },
    )
    listing = await entrypoint.module.ConnectionHubEntrypoint.delegated_access_list(
        entrypoint.instance
    )

    assert response["ok"] is True
    call = entrypoint.policies.set_calls[0]
    assert call["owner_subject"] == "user-1"
    assert call["authority"].to_dict() == {
        "access_id": "access-1",
        "resource": "urn:example:service",
        "surface": "outer",
        "operation": "records.delete",
    }
    assert call["expected_revision"] == 0
    assert listing["items"][0]["invocation_policies"][0]["mode"] == "once"


@pytest.mark.asyncio
async def test_policy_cannot_be_attached_to_an_operation_absent_from_the_card(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.delegated_invocation_policy_set(
        entrypoint.instance,
        data={
            "access_id": "access-1",
            "resource": "urn:example:service",
            "operation": "records.publish",
            "mode": "once",
        },
    )

    assert response == {
        "ok": False,
        "error": "invocation_policy_operation_not_granted",
        "status": 409,
    }
    assert entrypoint.policies.set_calls == []


@pytest.mark.asyncio
async def test_account_specific_policy_requires_that_exact_card_binding(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.delegated_invocation_policy_set(
        entrypoint.instance,
        data={
            "access_id": "access-1",
            "resource": "urn:example:service",
            "operation": "records.delete",
            "mode": "always",
            "account": {
                "provider_id": "slack",
                "account_id": "account-2",
            },
        },
    )

    assert response["error"] == "invocation_policy_account_not_granted"
    assert entrypoint.policies.set_calls == []


@pytest.mark.asyncio
async def test_manual_secret_card_sets_every_policy_before_returning_bearer(entrypoint):
    resource = (
        "urn:kdcube:management:secret:tenant-a:project-a:"
        "platform:_:platform.services.brave.api_key"
    )
    response = await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={
            "label": "Secret operator",
            "resource_grants": {resource: ["kdcube.management.secret.value.write"]},
            "resource_operations": {
                resource: [
                    "kdcube.management.secret.metadata.read",
                    "kdcube.management.secret.value.write",
                ]
            },
            "invocation_policies": {
                resource: {
                    "kdcube.management.secret.metadata.read": "once",
                    "kdcube.management.secret.value.write": "always",
                }
            },
        },
    )

    assert response["ok"] is True
    assert response["access_token"] == "not-returned-until-policies-exist"
    assert [call["mode"] for call in entrypoint.policies.set_calls] == [
        "once",
        "always",
    ]
    assert all(call["authority"].access_id == "aut-secret-1" for call in entrypoint.policies.set_calls)
    assert response["access"]["invocation_policies"]


@pytest.mark.asyncio
async def test_manual_secret_card_requires_modes_and_broad_scope_rejects_once(entrypoint):
    exact = (
        "urn:kdcube:management:secret:tenant-a:project-a:"
        "platform:_:platform.services.brave.api_key"
    )
    missing = await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={
            "resource_grants": {exact: ["kdcube.management.secret.value.write"]},
            "resource_operations": {
                exact: ["kdcube.management.secret.value.write"]
            },
        },
    )
    broad = exact.rsplit(".", 1)[0] + ".*"
    rejected = await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={
            "resource_grants": {broad: ["kdcube.management.secret.value.write"]},
            "resource_operations": {
                broad: ["kdcube.management.secret.value.write"]
            },
            "invocation_policies": {
                broad: {"kdcube.management.secret.value.write": "once"}
            },
        },
    )

    assert missing["error"] == "invalid_delegated_access_request"
    assert "require Once or Always" in missing["message"]
    assert rejected["error"] == "invalid_delegated_access_request"
    assert "broad_secret_selector_requires_always" in rejected["message"]
    assert entrypoint.access.create_calls == []

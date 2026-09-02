"""Delegated access create and recovery preserve the complete card contract."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)
from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.invocation_policy import (
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
    canonical_request_digest,
)

ACCOUNT_SCOPE = {"linkedin": {"linkedin_acc_1": ["linkedin:post"]}}


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_access(self, user, **kwargs):
        self.calls.append({"user": user, **kwargs})
        return {"ok": True}

    async def update_access(self, user, **kwargs):
        self.calls.append({"method": "update", "user": user, **kwargs})
        return {"ok": True}

    async def extend_client_access(self, user, **kwargs):
        self.calls.append({"method": "extend", "user": user, **kwargs})
        return {"ok": True}


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _RecordingService()
    monkeypatch.setattr(module, "_automation_access_service", lambda *a, **kw: service)
    monkeypatch.setattr(
        module, "_platform_user_payload", lambda *a, **kw: {"user_id": "google:1"}
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


def test_service_still_accepts_account_scope():
    # Guards the other side of the contract: a renamed/removed service
    # parameter must fail here, not silently drop bindings at runtime.
    assert "account_scope" in inspect.signature(AutomationAccessService.create_access).parameters


def test_service_accepts_resource_qualified_operations_on_every_grant_path():
    assert "resource_operations" in inspect.signature(
        AutomationAccessService.create_access
    ).parameters
    assert "resource_operations" in inspect.signature(
        AutomationAccessService.extend_client_access
    ).parameters
    assert "access_id" in inspect.signature(
        AutomationAccessService.extend_client_access
    ).parameters


@pytest.mark.asyncio
async def test_account_scope_is_forwarded_to_the_service(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={
            "label": "Automation access",
            "resource_grants": {"res": ["named_services:use"]},
            "account_scope": ACCOUNT_SCOPE,
        },
    )
    call = entrypoint.service.calls[-1]
    assert call["account_scope"] == ACCOUNT_SCOPE


@pytest.mark.asyncio
async def test_absent_account_scope_stays_none(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={"label": "x", "resource_grants": {"res": ["named_services:use"]}},
    )
    assert entrypoint.service.calls[-1]["account_scope"] is None


@pytest.mark.asyncio
async def test_update_forwards_explicit_empty_account_scope(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_update(
        entrypoint.instance,
        data={
            "access_id": "aut_1",
            "resource_grants": {"res": ["named_services:use"]},
            "account_scope": {},
        },
    )
    assert entrypoint.service.calls[-1]["method"] == "update"
    assert entrypoint.service.calls[-1]["account_scope"] == {}


@pytest.mark.asyncio
async def test_external_client_grant_forwards_explicit_empty_account_scope(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "external-client",
            "resource": "res",
            "claims": ["named_services:use"],
            "account_scope": {},
            "replace": True,
        },
    )
    assert entrypoint.service.calls[-1]["method"] == "extend"
    assert entrypoint.service.calls[-1]["account_scope"] == {}


@pytest.mark.asyncio
async def test_external_client_grant_forwards_exact_outer_operation(entrypoint):
    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "external-client",
            "resource": "https://service.example/mcp",
            "claims": [],
            "outer_operation": "records_export",
        },
    )

    assert result["ok"] is True
    call = entrypoint.service.calls[-1]
    assert call["method"] == "extend"
    assert call["resource_operations"] == {
        "https://service.example/mcp": ["records_export"]
    }


@pytest.mark.asyncio
async def test_external_client_recovery_forwards_exact_card_identity(entrypoint):
    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "automation:manual-caller",
            "access_id": "aut_exact_card",
            "resource": "https://service.example/mcp",
            "claims": [],
            "outer_operation": "records_export",
        },
    )

    assert result["ok"] is True
    call = entrypoint.service.calls[-1]
    assert call["method"] == "extend"
    assert call["access_id"] == "aut_exact_card"


@pytest.mark.asyncio
async def test_named_service_operations_keep_their_absent_semantics(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_create(
        entrypoint.instance,
        data={"label": "x", "resource_grants": {"res": ["named_services:use"]}},
    )
    assert entrypoint.service.calls[-1]["named_service_operations"] is None


@pytest.mark.asyncio
async def test_operation_only_agent_grant_authors_exact_operation(
    entrypoint, monkeypatch
):
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_to_kdcube import (
        consent_demand,
    )

    calls: list[dict] = []

    async def author(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(consent_demand, "author_consent_granted_events", author)
    monkeypatch.setattr(
        entrypoint.module,
        "_platform_user_payload",
        lambda *args, **kwargs: {"sub": "user-1", "user_id": "user-1"},
    )
    entrypoint.instance.redis = object()
    resource = "*/kdcube-services@1-0/public/mcp/named_services*"

    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "kdcube-agent:workspace@1-0:main",
            "resource": resource,
            "claims": [],
            "named_service_operations": {
                "slack": ["object.action.upload_file"]
            },
        },
    )

    assert result["ok"] is True
    assert calls[0]["granted_claims"] == []
    assert calls[0]["granted_named_service_operations"] == {
        "slack": ["object.action.upload_file"]
    }
    assert calls[0]["granted_resource"] == resource


@pytest.mark.asyncio
async def test_outer_operation_only_agent_grant_authors_exact_operation(
    entrypoint, monkeypatch
):
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_to_kdcube import (
        consent_demand,
    )

    calls: list[dict] = []

    async def author(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(consent_demand, "author_consent_granted_events", author)
    monkeypatch.setattr(
        entrypoint.module,
        "_platform_user_payload",
        lambda *args, **kwargs: {"sub": "user-1", "user_id": "user-1"},
    )
    entrypoint.instance.redis = object()
    resource = "https://service.example/mcp"

    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "kdcube-agent:workspace@1-0:main",
            "resource": resource,
            "claims": [],
            "outer_operation": "records_export",
        },
    )

    assert result["ok"] is True
    call = entrypoint.service.calls[-1]
    assert call["resource_operations"] == {resource: ["records_export"]}
    assert calls[0]["granted_resource_operations"] == {
        resource: ["records_export"]
    }
    assert calls[0]["granted_resource"] == resource


@pytest.mark.asyncio
async def test_agent_grant_without_any_authority_is_rejected(entrypoint):
    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "kdcube-agent:workspace@1-0:main",
            "resource": "*/kdcube-services@1-0/public/mcp/named_services*",
            "claims": [],
        },
    )

    assert result == {
        "ok": False,
        "error": "delegated_agent_grant_requires_resource_and_claims",
    }
    assert entrypoint.service.calls == []


@pytest.mark.asyncio
async def test_claim_only_agent_grant_remains_valid(entrypoint):
    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        entrypoint.instance,
        data={
            "client_id": "kdcube-agent:workspace@1-0:main",
            "resource": "*/kdcube-services@1-0/public/mcp/named_services*",
            "claims": ["named_services:use"],
        },
    )

    assert result["ok"] is True
    assert entrypoint.service.calls[-1]["resource_grants"] == {
        "*/kdcube-services@1-0/public/mcp/named_services*": [
            "named_services:use"
        ]
    }
    assert entrypoint.service.calls[-1]["named_service_operations"] is None


@pytest.mark.asyncio
async def test_operation_grant_and_once_policy_stay_fail_closed_until_both_commit(
    monkeypatch,
    tmp_path,
):
    module = _entrypoint_module()
    resource = "https://reference.example.test/customers"
    catalog_resource = f"{resource}*"
    access_id = "access_demo"
    client_id = "claude-code"

    class ExistingClientService:
        def __init__(self):
            self.extend_calls = 0
            self.card = {
                "access_id": access_id,
                "client_id": client_id,
                "resource_grants": {resource: ["external_mcp:use"]},
                "resource_operations": {resource: ["status"]},
                "catalog_row_by_resource": {resource: catalog_resource},
                "card_revision": 3,
            }

        async def list_access(self, user):
            assert user["sub"] == "user-1"
            return {
                "ok": True,
                "platform_user_id": "user-1",
                "items": [dict(self.card)],
                "resources": [
                    {
                        "resource": catalog_resource,
                        "operations": [
                            {"name": "status"},
                            {"name": "restart"},
                        ],
                    }
                ],
            }

        async def extend_client_access(self, user, **kwargs):
            assert user["sub"] == "user-1"
            assert kwargs["access_id"] == access_id
            self.extend_calls += 1
            selected = list(
                kwargs["resource_operations"].get(resource, [])
            )
            self.card["resource_operations"] = {
                resource: sorted(
                    set(self.card["resource_operations"][resource])
                    | set(selected)
                )
            }
            self.card["card_revision"] += 1
            return {
                "ok": True,
                "access_id": access_id,
                "access": dict(self.card),
            }

    class Locks:
        @staticmethod
        @asynccontextmanager
        async def lock(**_kwargs):
            yield {}

    policies = InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path),
        mutation_lock=Locks.lock,
    )

    class FailFirstCommit:
        def __init__(self):
            self.failed = False

        async def prepare_policy_change(self, **kwargs):
            return await policies.prepare_policy_change(**kwargs)

        async def commit_policy_change(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated interruption after card commit")
            return await policies.commit_policy_change(**kwargs)

        async def get(self, **kwargs):
            return await policies.get(**kwargs)

    access = ExistingClientService()
    policy_port = FailFirstCommit()
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_to_kdcube import (
        consent_demand,
    )

    authored: list[dict] = []

    async def author(**kwargs):
        authored.append(kwargs)
        return 1

    monkeypatch.setattr(consent_demand, "author_consent_granted_events", author)
    monkeypatch.setattr(module, "_automation_access_service", lambda *a, **kw: access)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda *a, **kw: policy_port)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"sub": "user-1", "user_id": "user-1"},
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = object()
    payload = {
        "client_id": client_id,
        "access_id": access_id,
        "resource": resource,
        "claims": [],
        "resource_operations": {resource: ["restart"]},
        "invocation_mode": POLICY_ONCE,
        "invocation_change_id": "grant-restart-1",
    }

    interrupted = await module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        instance,
        data=payload,
    )
    assert authored == []
    authority = InvocationAuthority(
        access_id=access_id,
        resource=resource,
        surface=SURFACE_OUTER,
        operation="restart",
    )
    blocked = await policies.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-restart-1",
        request_digest=canonical_request_digest({"service": "api"}),
    )
    completed = await module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        instance,
        data=payload,
    )
    replay = await module.ConnectionHubEntrypoint.delegated_agent_grant_create(
        instance,
        data=payload,
    )

    assert interrupted["error"] == "delegated_invocation_policy_unavailable"
    assert "restart" in access.card["resource_operations"][resource]
    assert blocked.reason == "delegated_invocation_policy_changing"
    assert completed["invocation_policy"]["mode"] == POLICY_ONCE
    assert completed["invocation_policy"]["remaining"] == 1
    assert replay["replay"] is True
    assert access.extend_calls == 2
    assert len(authored) == 2
    assert all(call["invocation_policy_mode"] == POLICY_ONCE for call in authored)
    assert all(call["invocation_change_id"] == "grant-restart-1" for call in authored)


# -- save preconditions ---------------------------------------------------------


def test_service_still_accepts_the_save_preconditions():
    parameters = inspect.signature(AutomationAccessService.update_access).parameters
    assert "expected_card_revision" in parameters
    assert "expected_catalog_version" in parameters


@pytest.mark.asyncio
async def test_update_forwards_the_editor_preconditions(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_update(
        entrypoint.instance,
        data={
            "access_id": "aut_1",
            "resource_grants": {"res": ["named_services:use"]},
            "expected_card_revision": 8,
            "expected_catalog_version": "delegated_catalog_2026-08-14-10-00-00-000_abcdef012345",
        },
    )
    call = entrypoint.service.calls[-1]
    assert call["expected_card_revision"] == 8
    assert call["expected_catalog_version"] == (
        "delegated_catalog_2026-08-14-10-00-00-000_abcdef012345"
    )


@pytest.mark.asyncio
async def test_a_revision_sent_as_a_string_is_accepted(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_update(
        entrypoint.instance,
        data={
            "access_id": "aut_1",
            "resource_grants": {"res": ["named_services:use"]},
            "expected_card_revision": "8",
        },
    )
    assert entrypoint.service.calls[-1]["expected_card_revision"] == 8


@pytest.mark.asyncio
async def test_absent_preconditions_stay_none(entrypoint):
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_update(
        entrypoint.instance,
        data={"access_id": "aut_1", "resource_grants": {"res": ["named_services:use"]}},
    )
    call = entrypoint.service.calls[-1]
    assert call["expected_card_revision"] is None
    assert call["expected_catalog_version"] is None


@pytest.mark.asyncio
async def test_a_malformed_revision_is_a_bad_request_not_a_conflict(entrypoint):
    """A conflict means the editor is stale; a broken field is neither."""
    result = await entrypoint.module.ConnectionHubEntrypoint.delegated_access_update(
        entrypoint.instance,
        data={
            "access_id": "aut_1",
            "resource_grants": {"res": ["named_services:use"]},
            "expected_card_revision": "not-a-number",
        },
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_delegated_access_request"
    assert entrypoint.service.calls == []

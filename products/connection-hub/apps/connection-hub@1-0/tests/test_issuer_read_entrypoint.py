"""Author hosting-handler tests, NOT mounted platform authentication proof."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


def body():
    return {"context_ref": '{"purpose":"identity"}', "request_id": "read-1", "targets": [
        {"owner_subject": f"owner-{i}", "access_id": f"card-{i}", "issuer_kind": "opaque", "issuer_ref": "lineage"}
        for i in range(2)]}


def entry(m, *, classification="registered", authority=None, scope=None):
    e = m.ConnectionHubEntrypoint.__new__(m.ConnectionHubEntrypoint)
    e.comm_context = SimpleNamespace(user=SimpleNamespace(user_id="actual-human", user_type=classification,
        username="human", identity_authority=authority or {}))
    e.runtime_identity = lambda: {"tenant": "actual-tenant", "project": "actual-project"} if scope is None else scope
    return e


@pytest.mark.asyncio
async def test_host_binds_actual_human_classification_scope_and_ignores_sdk_metadata(monkeypatch):
    m = module()
    service = MagicMock()
    service.issuer_managed_lifecycle_read = AsyncMock(return_value={"ok": True})
    factory = AsyncMock(return_value=service)
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_lifecycle_read(entry(m),
        user_id="forged", fingerprint="forged", **body())
    assert result == {"ok": True}
    service.bind_issuer_read_registry.assert_called_once_with(service._issuer_reads,
        actor_subject="actual-human", actor_classification="registered", tenant="actual-tenant", project="actual-project")
    service.issuer_managed_lifecycle_read.assert_awaited_once_with(body())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["actor_subject", "actor_classification", "tenant", "project", "read_digest",
                                   "identity_leaf_paths", "user_id", "fingerprint", "approval_context"])
async def test_caller_fields_cannot_supply_authority_or_exports(monkeypatch, field):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_lifecycle_read(entry(m), data={**body(), field: "forged"})
    assert result["ok"] is False and result["status"] == 400
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"classification": "external"}, {"classification": "anonymous"},
    {"authority": {"authority_id": "delegated_client", "grantor_user_id": "actual-human"}},
    {"authority": {"delegated_card_binding": {"access_id": "owner-equal"}}},
    {"scope": {}}, {"scope": {"tenant": "t", "project": ""}}])
async def test_missing_human_classification_or_actual_scope_refuses_before_service(monkeypatch, kwargs):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_lifecycle_read(entry(m, **kwargs), data=body())
    assert result["ok"] is False and result["status"] == 403
    factory.assert_not_called()


def test_read_alias_is_explicit_registered_privileged_post_operations_csrf():
    m = module()
    method = m.ConnectionHubEntrypoint.issuer_managed_lifecycle_read.__bundle_api_method__
    assert method.alias == "issuer_managed_lifecycle_read" and method.http_method == "POST"
    assert method.route == "operations"
    assert method.alias in m.CSRF_PROTECTED_OPERATION_ALIASES


@pytest.mark.asyncio
async def test_read_factory_requires_separate_descriptor_operation_and_signs_exact_full_payload():
    from connection_hub.delegated_credentials.issuer_read import (
        IssuerReadQuery, IssuerReadRequest, IssuerReadRefused, read_digest, verify_issuer_read_request,
    )
    m = module()
    q = IssuerReadQuery.from_mapping(body())
    request = IssuerReadRequest("human", "registered", "t", "p", q.context_ref, q.request_id, q.targets)
    config = {"delegated_credentials": {"issuer_authorities": {"opaque": {
        "bundle_id": "owner@1-0", "operation": "write", "adapter_id": "owner-v1",
        "service_id": "hub", "peer_proof_secret_ref": "fixture.secret", "read_operation": "read",
        "read_identity_leaf_paths": [["properties", "identity", "subject"]]}}}}
    resolver = AsyncMock(return_value="x" * 32)
    async def caller(**kwargs):
        assert kwargs["operation"] == "read" and kwargs["route"] == "public"
        payload = kwargs["data"]
        assert verify_issuer_read_request(secret="x" * 32, bundle_id="owner@1-0", operation="read",
            expected_service_id="hub", body=payload).allowed
        return {"ok": True, "request": payload["request"], "phase": payload["phase"],
            "snapshots_digest": read_digest(payload["snapshots"]), "decision": {
                "allowed": True, "reason": "", "policy_version": "v1",
                "valid_until": "2099-01-01T00:00:00+00:00"}}
    registry = m.issuer_read_registry_from_connections(connections=config, resolve_secret=resolver, caller=caller)
    # Wire reached correct peer; oversized decision window is still rejected.
    with pytest.raises(IssuerReadRefused, match="response_invalid"):
        await registry.decide(request, issuer_kind="opaque")
    resolver.assert_awaited_once_with("fixture.secret")
    del config["delegated_credentials"]["issuer_authorities"]["opaque"]["read_operation"]
    unavailable = m.issuer_read_registry_from_connections(connections=config, resolve_secret=resolver, caller=caller)
    with pytest.raises(IssuerReadRefused, match="adapter_unavailable"):
        await unavailable.decide(request, issuer_kind="opaque")

"""Author host wiring tests; not mounted human authentication or issuer replay proof."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.issuer_read import (
    IssuerReadQuery, read_digest, verify_issuer_read_request,
)
from connection_hub.delegated_credentials.issuer_snapshot import (
    IssuerSnapshotRefused, IssuerSnapshotRequest, verify_issuer_snapshot_request,
)
from test_issuer_read_entrypoint import body, entry, module


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["registered", "privileged"])
async def test_snapshot_host_uses_actual_context_not_sdk_metadata(monkeypatch, classification):
    m = module()
    service = MagicMock()
    service.issuer_managed_card_snapshots = AsyncMock(return_value={"ok": True})
    factory = AsyncMock(return_value=service)
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_snapshots(
        entry(m, classification=classification), user_id="forged", fingerprint="forged", **body())
    assert result == {"ok": True}
    service.bind_issuer_snapshot_registry.assert_called_once_with(service._issuer_snapshots,
        actor_subject="actual-human", actor_classification=classification,
        tenant="actual-tenant", project="actual-project")
    service.issuer_managed_card_snapshots.assert_awaited_once_with(body())
    service.bind_issuer_read_registry.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["actor_subject", "actor_classification", "tenant", "project",
    "snapshot_digest", "read_digest", "user_id", "fingerprint", "approval_context", "full_snapshot_operation"])
async def test_snapshot_query_cannot_supply_authority(monkeypatch, field):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_snapshots(
        entry(m), data={**body(), field: "forged"})
    assert result == {"ok": False, "status": 400, "error": "issuer_snapshot_query_invalid"}
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"classification": "external"}, {"classification": "anonymous"},
    {"authority": {"authority_id": "delegated_client", "grantor_user_id": "actual-human"}},
    {"authority": {"delegated_card_binding": {"access_id": "owner-equal"}}},
    {"scope": {}}, {"scope": {"tenant": "t", "project": ""}}])
async def test_snapshot_requires_platform_human_and_real_scope_before_factory(monkeypatch, kwargs):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_snapshots(entry(m, **kwargs), data=body())
    assert result["status"] == 403 and result["ok"] is False
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["actor", "classification", "tenant", "project"])
async def test_context_movement_during_service_construction_refuses_before_export(monkeypatch, field):
    m = module()
    e = entry(m)
    scope = {"tenant": "actual-tenant", "project": "actual-project"}
    e.runtime_identity = lambda: scope
    service = MagicMock()
    service.issuer_managed_card_snapshots = AsyncMock()

    async def factory(*args):
        if field == "actor":
            e.comm_context.user.user_id = "replacement-human"
        elif field == "classification":
            e.comm_context.user.user_type = "external"
        else:
            scope[field] = "replacement-scope"
        return service

    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_snapshots(e, data=body())
    assert result == {"ok": False, "error": "issuer_snapshot_context_changed", "status": 409, "retryable": True}
    service.bind_issuer_snapshot_registry.assert_not_called()
    service.issuer_managed_card_snapshots.assert_not_called()


@pytest.mark.asyncio
async def test_missing_snapshot_host_port_is_a_named_refusal(monkeypatch):
    m = module()
    monkeypatch.setattr(m, "_automation_access_service", AsyncMock(return_value=object()))
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_snapshots(entry(m), data=body())
    assert result == {"ok": False, "error": "issuer_snapshot_host_unavailable", "status": 503, "retryable": True}


def test_snapshot_alias_is_protected_post_operations():
    m = module()
    method = m.ConnectionHubEntrypoint.issuer_managed_card_snapshots.__bundle_api_method__
    assert method.alias == "issuer_managed_card_snapshots" and method.http_method == "POST"
    assert method.route == "operations"
    assert method.alias in m.CSRF_PROTECTED_OPERATION_ALIASES


def descriptor():
    return {"delegated_credentials": {"issuer_authorities": {"opaque": {
        "bundle_id": "owner@1-0", "operation": "write", "read_operation": "identity-read",
        "full_snapshot_operation": "full-export", "adapter_id": "owner-v1",
        "service_id": "hub", "peer_proof_secret_ref": "fixture.secret"}}}}


def snapshot_request():
    q = IssuerReadQuery.from_mapping(body())
    return IssuerSnapshotRequest("human", "registered", "t", "p", q.context_ref, q.request_id, q.targets)


@pytest.mark.asyncio
async def test_separate_snapshot_factory_signs_authorize_and_exact_full_validation():
    m = module()
    config = descriptor()
    request = snapshot_request()
    resolver = AsyncMock(return_value="x" * 32)
    calls = []

    async def caller(**kwargs):
        assert kwargs["operation"] == "full-export" and kwargs["route"] == "public"
        assert kwargs["http_method"] == "POST" and kwargs["bundle_id"] == "owner@1-0"
        payload = kwargs["data"]
        assert verify_issuer_snapshot_request(secret="x" * 32, bundle_id="owner@1-0",
            operation="full-export", expected_service_id="hub", body=payload).allowed
        assert not verify_issuer_read_request(secret="x" * 32, bundle_id="owner@1-0",
            operation="full-export", expected_service_id="hub", body=payload).allowed
        calls.append(payload)
        return {"ok": True, "request": payload["request"], "phase": payload["phase"],
            "snapshots_digest": read_digest(payload["snapshots"]), "decision": {
                "allowed": True, "reason": "", "policy_version": "v1",
                "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}}

    registry = m.issuer_snapshot_registry_from_connections(connections=config, resolve_secret=resolver, caller=caller)
    # Frozen rows cannot move the request to a different peer after construction.
    config["delegated_credentials"]["issuer_authorities"]["opaque"]["full_snapshot_operation"] = "replacement"
    first = await registry.decide(request, issuer_kind="opaque")
    cards = [CardAuthority(access_id=t.access_id, grantor_subject=t.owner_subject, source="control",
        client_id="", delegate_subject="", card_kind="control", card_revision=1, state="active",
        issuer_kind=t.issuer_kind, issuer_ref=t.issuer_ref, resource_grants={}, resource_operations={},
        named_service_operations=NamedServiceSelection.none()) for t in request.targets]
    snapshots = [{"target": t.to_dict(), "card_revision": c.card_revision,
        "authority_fingerprint": c.content_hash(), "authority": c.to_dict()} for t, c in zip(request.targets, cards)]
    fresh = await registry.revalidate(request, first, snapshots=snapshots)
    registry.require(request, fresh, issuer_kind="opaque", phase="validate", snapshots=snapshots)
    assert calls[0]["snapshots"] == [] and calls[1]["snapshots"] == snapshots
    assert calls[0]["request"] == calls[1]["request"] == request.to_dict()
    assert calls[0]["service_proof"]["nonce"] != calls[1]["service_proof"]["nonce"]
    assert resolver.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", " ", False, {}])
async def test_identity_or_write_capability_never_implies_full_export(value):
    m = module()
    config = descriptor()
    row = config["delegated_credentials"]["issuer_authorities"]["opaque"]
    if value is None:
        del row["full_snapshot_operation"]
    else:
        row["full_snapshot_operation"] = value
    resolver, caller = AsyncMock(), AsyncMock()
    registry = m.issuer_snapshot_registry_from_connections(connections=config, resolve_secret=resolver, caller=caller)
    with pytest.raises(IssuerSnapshotRefused, match="adapter_unavailable"):
        await registry.decide(snapshot_request(), issuer_kind="opaque")
    resolver.assert_not_called()
    caller.assert_not_called()


@pytest.mark.asyncio
async def test_missing_existing_peer_secret_refuses_without_calling_provider():
    m = module()
    caller = AsyncMock()
    registry = m.issuer_snapshot_registry_from_connections(connections=descriptor(),
        resolve_secret=AsyncMock(return_value=""), caller=caller)
    with pytest.raises(IssuerSnapshotRefused, match="adapter_unavailable"):
        await registry.decide(snapshot_request(), issuer_kind="opaque")
    caller.assert_not_called()

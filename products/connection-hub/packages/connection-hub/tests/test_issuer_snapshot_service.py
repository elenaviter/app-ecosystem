"""Author service binding tests for the separate full-snapshot capability."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.issuer_read import IssuerReadRegistry
from connection_hub.delegated_credentials.issuer_snapshot import IssuerSnapshotRegistry
from test_issuer_snapshot import HOST, query
from test_lifecycle_store import _pair
from connection_hub.delegated_credentials.issuer_snapshot_host import (
    bind_issuer_snapshot_orchestration, issuer_snapshot_orchestration_is_bound,
)


def test_delivery_scope_defaults_closed_and_restores_nested_scope_after_exception():
    assert not issuer_snapshot_orchestration_is_bound()
    with bind_issuer_snapshot_orchestration():
        assert issuer_snapshot_orchestration_is_bound()
        with pytest.raises(RuntimeError):
            with bind_issuer_snapshot_orchestration():
                raise RuntimeError("fixture")
        assert issuer_snapshot_orchestration_is_bound()
    assert not issuer_snapshot_orchestration_is_bound()


@pytest.mark.asyncio
async def test_delivery_scope_is_task_local_and_revoked_in_inherited_context():
    import asyncio
    release = asyncio.Event()

    async def child():
        assert issuer_snapshot_orchestration_is_bound()
        await release.wait()
        return issuer_snapshot_orchestration_is_bound()

    with bind_issuer_snapshot_orchestration():
        task = asyncio.create_task(child())
        await asyncio.sleep(0)
    assert not issuer_snapshot_orchestration_is_bound()
    release.set()
    assert await asyncio.wait_for(task, 1) is False


def service():
    instance = AutomationAccessService.__new__(AutomationAccessService)
    instance._persistence = MagicMock()
    instance._persistence.read_lifecycle_identities = AsyncMock(return_value=_pair())
    return instance


@pytest.mark.asyncio
async def test_unbound_or_identity_only_service_cannot_export_full_authorities():
    instance = service()
    instance.bind_issuer_read_registry(IssuerReadRegistry(), **HOST)
    result = await instance.issuer_managed_card_snapshots(query().to_dict())
    assert result == {"ok": False, "status": 403, "error": "issuer_snapshot_host_unavailable", "retryable": False}
    instance._persistence.read_lifecycle_identities.assert_not_called()


def test_identity_read_registry_cannot_be_bound_as_full_snapshot_registry():
    with pytest.raises(ValueError, match="issuer_snapshot_registry_invalid"):
        service().bind_issuer_snapshot_registry(IssuerReadRegistry(), **HOST)


@pytest.mark.asyncio
async def test_service_uses_its_bound_actor_scope_and_full_original_payload():
    instance = service()
    registry = IssuerSnapshotRegistry()
    calls = []

    class Adapter:
        adapter_id = "fixture-full-export"

        def __init__(self, kind):
            self.issuer_kind = kind

        async def decide_snapshot(self, request, *, phase, snapshots):
            assert (request.actor_subject, request.actor_classification, request.tenant, request.project) == tuple(HOST.values())
            calls.append((phase, snapshots))
            return True, "", "v1", datetime.now(timezone.utc) + timedelta(seconds=30)

    for card in _pair():
        registry.register(Adapter(card.issuer_kind))
    instance.bind_issuer_snapshot_registry(registry, **HOST)
    result = await instance.issuer_managed_card_snapshots(query().to_dict())
    assert result["ok"] and result["status"] == 200
    assert [s["authority"] for s in result["snapshots"]] == [c.to_dict() for c in _pair()]
    assert [s["authority_fingerprint"] for s in result["snapshots"]] == [c.content_hash() for c in _pair()]
    assert [phase for phase, _ in calls] == ["authorize", "authorize", "validate", "validate"]
    assert all(snapshots == result["snapshots"] for phase, snapshots in calls if phase == "validate")
    assert instance._persistence.read_lifecycle_identities.await_count == 1


@pytest.mark.asyncio
async def test_caller_actor_or_missing_storage_refuses_without_payload_export():
    instance = service()
    instance.bind_issuer_snapshot_registry(IssuerSnapshotRegistry(), **HOST)
    result = await instance.issuer_managed_card_snapshots({**query().to_dict(), "actor_subject": "forged"})
    assert result["error"] == "issuer_snapshot_query_invalid" and "snapshots" not in result
    instance._persistence.read_lifecycle_identities.assert_not_called()
    instance._persistence = None
    result = await instance.issuer_managed_card_snapshots(query().to_dict())
    assert result["error"] == "issuer_snapshot_port_unavailable" and "snapshots" not in result

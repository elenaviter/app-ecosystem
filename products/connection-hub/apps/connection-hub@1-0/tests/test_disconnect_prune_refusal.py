"""Real app-handler ordering tests, not mounted or atomicity proof."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from test_issuer_read_entrypoint import entry, module


def authenticated_entry(m):
    return entry(m, authority={"platform_user_id": "actual-human"})


def host(monkeypatch, *, account=SimpleNamespace(provider_id="slack"), prune=None):
    m = module()
    operations = MagicMock()
    operations.store.get_account = AsyncMock(return_value=account)
    operations.disconnect = AsyncMock(return_value={
        "ok": True, "removed": True, "account_id": "account-1",
    })
    factory = MagicMock(return_value=operations)
    service = MagicMock()
    service.prune_account_from_grants = AsyncMock(return_value=prune)
    # W578: with Card transactions off the transactional disconnect declines (None): the ordered path runs.
    service.disconnect_account_in_transaction = AsyncMock(return_value=None)
    service_factory = AsyncMock(return_value=service)
    monkeypatch.setattr(m, "_delegated_to_kdcube_operations", factory)
    monkeypatch.setattr(m, "_automation_access_service", service_factory)
    return m, operations, factory, service, service_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("prune", [
    {"ok": False, "pruned": 0, "grants": [], "not_pruned": [], "reason": "grants_unreadable"},
    {"ok": False, "pruned": 1, "grants": ["done"], "not_pruned": ["blocked"]},
    {"ok": True, "pruned": 0, "grants": [], "not_pruned": ["blocked"]},
    {"pruned": 0, "grants": [], "not_pruned": []},  # Old package response is insufficient.
    {"ok": 1, "pruned": 0, "grants": [], "not_pruned": []},
    {"ok": True, "pruned": 0, "grants": []},
    {"ok": True, "pruned": 0, "grants": [], "not_pruned": None},
    {"ok": True, "pruned": -1, "grants": [], "not_pruned": []},
    {"ok": True, "pruned": True, "grants": ["done"], "not_pruned": []},
    {"ok": True, "pruned": 1, "grants": [], "not_pruned": []},
    {"ok": True, "pruned": 2, "grants": ["done", "done"], "not_pruned": []},
    {"ok": True, "pruned": 1, "grants": [""], "not_pruned": []},
    None, [], "unavailable",
])
async def test_failed_partial_or_malformed_pruning_never_disconnects(monkeypatch, prune):
    m, operations, _, service, _ = host(monkeypatch, prune=prune)
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        authenticated_entry(m), account_id="account-1",
    )
    assert result["ok"] is False
    assert result["removed"] is False
    assert result["error"] == "account_binding_not_pruned"
    assert result["retryable"] is True
    service.prune_account_from_grants.assert_awaited_once()
    operations.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["operations", "read", "service", "prune"])
async def test_unavailable_dependency_refuses_without_deleting_account(monkeypatch, failure):
    m, operations, factory, service, service_factory = host(monkeypatch)
    dependency = {"operations": factory, "read": operations.store.get_account,
                  "service": service_factory,
                  "prune": service.prune_account_from_grants}[failure]
    dependency.side_effect = RuntimeError("private diagnostic must not reach response")
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        authenticated_entry(m), account_id="account-1",
    )
    assert result["ok"] is False and result["removed"] is False
    assert result["error"] == "account_binding_not_pruned" and result["retryable"] is True
    assert "private diagnostic" not in str(result)
    operations.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["read", "prune"])
async def test_cancellation_propagates_before_disconnect(monkeypatch, dependency):
    m, operations, _, service, _ = host(monkeypatch)
    pending = operations.store.get_account if dependency == "read" else service.prune_account_from_grants
    pending.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(authenticated_entry(m), account_id="account-1")
    operations.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "", "  ", 42])
async def test_provider_cannot_be_supplied_by_caller_when_store_cannot_identify_it(monkeypatch, provider):
    m, operations, _, service, service_factory = host(
        monkeypatch, account=SimpleNamespace(provider_id=provider),
    )
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        authenticated_entry(m), data={"provider": "forged", "account_id": "account-1"},
    )
    assert result["ok"] is False and result["retryable"] is True
    operations.disconnect.assert_not_awaited()
    service_factory.assert_not_awaited()
    service.prune_account_from_grants.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_account_is_a_non_mutating_not_removed_result(monkeypatch):
    m, operations, _, _, service_factory = host(monkeypatch, account=None)
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(authenticated_entry(m), account_id="account-1")
    assert result == {"ok": False, "removed": False, "account_id": "account-1"}
    operations.disconnect.assert_not_awaited()
    service_factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [[], ["grant-1", "grant-2"]])
async def test_verified_pruning_precedes_disconnect_with_actual_actor_and_success_metadata(monkeypatch, grants):
    pruned = {"ok": True, "pruned": len(grants), "grants": grants, "not_pruned": []}
    m, operations, factory, service, service_factory = host(monkeypatch, prune=pruned)
    request = object()
    calls = []

    async def prune(**kwargs):
        calls.append("prune")
        return pruned

    async def disconnect(**kwargs):
        assert calls == ["prune"]
        calls.append("disconnect")
        return {"ok": True, "removed": True, "account_id": "account-1"}

    service.prune_account_from_grants.side_effect = prune
    operations.disconnect.side_effect = disconnect
    e = authenticated_entry(m)
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        e, data={"account_id": "account-1", "provider": "forged"},
        user_id="forged", request=request,
    )
    factory.assert_called_once_with(e, "actual-human")
    service_factory.assert_awaited_once_with(e, request)
    service.prune_account_from_grants.assert_awaited_once_with(
        grantor_subject="actual-human", provider_id="slack", account_id="account-1",
    )
    operations.disconnect.assert_awaited_once_with(account_id="account-1")
    assert calls == ["prune", "disconnect"]
    assert result["removed"] is True
    if grants:
        assert result["bindings_cleared"] == len(grants)
        assert result["bindings_cleared_grants"] == grants
    else:
        assert "bindings_cleared" not in result


@pytest.mark.asyncio
async def test_disconnect_failure_after_successful_prune_is_not_reported_as_success(monkeypatch):
    m, operations, _, _, _ = host(monkeypatch, prune={
        "ok": True, "pruned": 0, "grants": [], "not_pruned": [],
    })
    operations.disconnect.side_effect = RuntimeError("disconnect failed")
    with pytest.raises(RuntimeError, match="disconnect failed"):
        await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(authenticated_entry(m), account_id="account-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated,account_id,error", [
    (False, "account-1", "delegated_to_kdcube_requires_authenticated_user"),
    (True, "", "account_id_required"),
])
async def test_authentication_and_account_validation_precede_dependencies(monkeypatch, authenticated, account_id, error):
    m, operations, factory, _, service_factory = host(monkeypatch)
    e = authenticated_entry(m)
    if not authenticated:
        e.comm_context.user.user_id = ""
        e.comm_context.user.identity_authority = {}
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(e, account_id=account_id)
    assert result["ok"] is False and result["error"] == error
    factory.assert_not_called()
    operations.disconnect.assert_not_awaited()
    service_factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"ok": True, "removed": True, "bindings_cleared": 1, "bindings_cleared_grants": ["grant-1"]},
    {"ok": False, "error": "card_transaction_pending", "removed": False, "retryable": True, "status": 503},
])
async def test_w578_with_card_transactions_the_group_transaction_answers_and_the_ordered_path_never_runs(
        monkeypatch, answer):
    m, operations, _, service, _ = host(monkeypatch)
    service.disconnect_account_in_transaction = AsyncMock(return_value=answer)
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        authenticated_entry(m), account_id="account-1",
    )
    assert result == {**answer, "account_id": "account-1"}
    service.disconnect_account_in_transaction.assert_awaited_once_with(
        grantor_subject="actual-human", provider_id="slack", account_id="account-1")
    service.prune_account_from_grants.assert_not_awaited()
    operations.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_w578_a_failing_group_transaction_refuses_without_the_ordered_path(monkeypatch):
    m, operations, _, service, _ = host(monkeypatch)
    service.disconnect_account_in_transaction = AsyncMock(side_effect=RuntimeError("down"))
    result = await m.ConnectionHubEntrypoint.delegated_to_kdcube_disconnect(
        authenticated_entry(m), account_id="account-1",
    )
    assert result["error"] == "account_binding_not_pruned" and result["reason"] == "account_transaction_unavailable"
    assert result["removed"] is False and result["status"] == 503
    service.prune_account_from_grants.assert_not_awaited()
    operations.disconnect.assert_not_awaited()


def test_w578_every_account_store_the_app_builds_carries_the_shared_account_lock(tmp_path):
    m = module()
    entrypoint = SimpleNamespace(bundle_storage_root=lambda: tmp_path)
    store = m._delegated_to_kdcube_store(entrypoint, "user-1")
    assert store._account_lock is not None
    lock = store._account_lock("user-1", "account-1")
    assert hasattr(lock, "__aenter__")  # the production observed file lock
    without_storage = m._delegated_to_kdcube_store(SimpleNamespace(bundle_storage_root=lambda: None), "user-1")
    assert without_storage._account_lock is None

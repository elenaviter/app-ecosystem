"""Real registry/remote-adapter sealing across the portable lifecycle port.

The transport and serving stores are fixtures, not mounted/Redis/PG proof.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from connection_hub.delegated_credentials.remote_issuer import RemoteIssuerAdapter
from test_lifecycle_store import ACTOR, _pair, _wire, _seed, _states, _service_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "initial-denial", "policy-moved", "expiry-under-fences"])
async def test_actual_port_binds_same_pair_evidence_and_revalidates_both_sealed_issuers(tmp_path, failure):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    body = _wire(cards)
    lifecycle = LifecycleRequest.from_mapping(body)
    clock = [datetime.now(timezone.utc)]
    held, order, calls = [], [], []

    @asynccontextmanager
    async def locks(**kwargs):
        held.append(kwargs["resource_id"])
        order.append(kwargs["resource_id"])
        if failure == "expiry-under-fences" and len(held) == 3:
            clock[0] += timedelta(seconds=31)
        try:
            yield {}
        finally:
            held.pop()

    async def transport(request):
        calls.append(dict(request))
        assert request["actor_subject"] == ACTOR
        assert request["context_ref"] == body["context_ref"]
        assert request["request_id"] == body["request_id"]
        assert request["change_digest"] == body["change_digest"]
        assert request["action"] == "revoke"
        target = next(t for t in lifecycle.targets if t.access_id == request["access_id"])
        assert (request["card_revision"], request["issuer_kind"], request["issuer_ref"]) == (
            target.expected_card_revision, target.issuer_kind, target.issuer_ref)
        if len(calls) > 2:
            assert len(held) == 3
        return {"ok": True, "request": request, "decision": {
            "allowed": failure != "initial-denial", "reason": "fixture_policy_denied" if failure == "initial-denial" else "",
            "policy_version": "moved" if failure == "policy-moved" and len(calls) > 2 else "v1",
            "valid_until": (clock[0] + timedelta(seconds=30)).isoformat()}}

    registry = IssuerRegistry(now=lambda: clock[0])
    for target in lifecycle.targets:
        registry.register(RemoteIssuerAdapter(issuer_kind=target.issuer_kind, adapter_id=f"peer:{target.issuer_kind}",
                                               transport=transport))
    handles = MagicMock()
    handles.remove = AsyncMock()
    persistence = DurableCardPersistence(redis=MagicMock(), tenant="fixture", project="fixture", card_store=store,
        mutation_lock=locks, credential_handles=handles, authority_backend="postgresql")
    cache = _service_cache()
    persistence._cards._cache = cache
    port = AutomationAccessService(redis=object(), tenant="fixture", project="fixture", config=None,
        grant_store=object(), card_persistence=persistence)
    port.bind_issuer_registry(registry, actor_subject=ACTOR)
    result = await port.issuer_managed_lifecycle_apply(body)
    if not failure:
        assert result["state"] == "committed" and result["serving_state"] == "complete" and result["status"] == 200
        assert await _states(store, lifecycle) == [("revoked", 2), ("revoked", 2)]
        assert len(calls) == 6 and handles.remove.await_count == 2
        calls_before = list(calls)
        assert await port.issuer_managed_lifecycle_apply(body) == result
        assert calls == calls_before and handles.remove.await_count == 2
    else:
        assert result["status"] == 403
        assert result["reason"] == {"initial-denial": "fixture_policy_denied", "policy-moved": "issuer_policy_changed",
                                     "expiry-under-fences": "issuer_decision_expired"}[failure]
        assert await _states(store, lifecycle) == [("active", 1), ("active", 1)]
        handles.remove.assert_not_called()
        cache.claim_lifecycle.assert_not_called()
        cache.reconcile_projection.assert_not_called()
        assert await persistence.load_lifecycle_receipt(lifecycle, actor_subject=ACTOR) is None
    assert held == []

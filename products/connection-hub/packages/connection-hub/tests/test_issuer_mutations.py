"""Target-lock and actual-candidate regressions for the portable issuer gate."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_gate import (
    IssuerRegistry, IssuerRequest, IssuerWriteRefused, change_digest, issuer_write_refusal,
)
from connection_hub.delegated_credentials.remote_issuer import RemoteIssuerAdapter
from test_card_service import _Cache, _authority, SUBJECT_HASH, NOW
from test_project_control_cards import _regular_control, _Redis


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "revoke"])
@pytest.mark.parametrize("failure", ["timeout", "transport", "policy", "expiry"])
async def test_revalidation_refuses_inside_lock_before_any_effect_and_releases(tmp_path, action, failure):
    held = False
    released = 0

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        nonlocal held, released
        assert not held
        held = True
        try:
            yield
        finally:
            held = False
            released += 1

    authority = replace(_authority(), issuer_kind="opaque", issuer_ref="authority:record")
    store = BundleStorageDelegatedCardStore(tmp_path)
    cache = _Cache()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=mutation_lock)
    await service.commit(authority, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    effects = []
    for method in ("read", "reconcile_projection", "claim_transition", "commit_projection",
                   "commit_tombstone", "finalize_removal", "index_add", "index_remove"):
        original = getattr(cache, method)

        async def spy(*args, _method=method, _original=original, **kwargs):
            effects.append(_method)
            return await _original(*args, **kwargs)

        setattr(cache, method, spy)
    clock = [datetime.now(timezone.utc)]
    calls = 0

    async def transport(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            assert held
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "transport":
                raise RuntimeError("private transport details")
            if failure == "expiry":
                clock[0] += timedelta(seconds=31)
        return {"ok": True, "request": request, "decision": {
            "allowed": True, "reason": "", "policy_version": "changed" if calls > 1 and failure == "policy" else "v1",
            "valid_until": (clock[0] + timedelta(seconds=30)).isoformat(),
        }}

    registry = IssuerRegistry(now=lambda: clock[0])
    registry.register(RemoteIssuerAdapter(issuer_kind="opaque", adapter_id="peer", transport=transport, timeout_seconds=0.01))
    intent = {"action": "revoke", "access_id": authority.access_id, "card_revision": 1}
    candidate = replace(authority, card_revision=2, label="updated").to_dict() if action == "update" else intent
    request = IssuerRequest("authenticated-actor", "request-1", action, authority.access_id,
                            1, "opaque", "authority:record", change_digest(candidate), "server-reservation")
    issued = await registry.decide(request)
    assert issued.allowed and not held

    async def guard():
        fresh = await registry.revalidate(request, issued)
        refusal = issuer_write_refusal(authority, request, fresh, now=clock[0])
        if refusal:
            raise IssuerWriteRefused(refusal["reason"])

    with pytest.raises(IssuerWriteRefused):
        if action == "update":
            await service.commit(replace(authority, card_revision=2, label="updated"),
                                 subject_hash=SUBJECT_HASH, expected_revision=1, now=NOW, before_commit=guard)
        else:
            await service.revoke(subject_hash=SUBJECT_HASH, access_id=authority.access_id,
                                 expected_revision=1, before_commit=guard)
    current = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=authority.access_id)
    assert current[1] == authority
    assert effects == [] and not held and released == 2
    # A refusal did not retain the target lock.
    async with mutation_lock():
        assert held


@pytest.mark.asyncio
async def test_legacy_managed_snapshot_refuses_without_repair_or_any_write():
    legacy = replace(_regular_control(), issuer_kind="external-authority", properties={})
    persistence = object()
    service = AutomationAccessService(redis=_Redis(), tenant="tenant", project="project",
                                      config=None, grant_store=object(), card_persistence=persistence)
    service._load_record = AsyncMock(return_value=record_from_card(legacy))
    service._ensure_control_snapshot = AsyncMock(side_effect=AssertionError("migration must not run"))
    service._persist_record = AsyncMock(side_effect=AssertionError("write must not run"))
    result = await service.update_access({"user_id": legacy.grantor_subject, "roles": ["admin"]},
                                         access_id=legacy.access_id, resource_grants={})
    assert result["reason"] == "issuer_snapshot_requires_explicit_migration"
    service._ensure_control_snapshot.assert_not_called()
    service._persist_record.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_requires_actual_candidate_digest_and_revoke_intent():
    adapter = RemoteIssuerAdapter(issuer_kind="opaque", adapter_id="peer", transport=AsyncMock(),
                                  prepare_transport=AsyncMock())
    registry = IssuerRegistry()
    registry.register(adapter)
    intent = {"action": "revoke", "access_id": "card-1", "card_revision": 3}
    request = IssuerRequest("actor", "request", "revoke", "card-1", 3, "opaque", "record", change_digest(intent), "")
    with pytest.raises(IssuerWriteRefused, match="issuer_candidate_invalid"):
        await registry.prepare(request, current={}, candidate={"change_digest": request.change_digest})
    adapter._prepare_transport.assert_not_called()
    adapter._prepare_transport.return_value = {"ok": True, "request": asdict(request),
                                               "change_digest": request.change_digest, "context_ref": "server-context"}
    prepared = await registry.prepare(request, current={"access_id": "card-1"}, candidate=intent)
    assert prepared.context_ref == "server-context"
    assert adapter._prepare_transport.call_args.args[0]["candidate"] == intent


@pytest.mark.asyncio
async def test_finalize_failure_is_explicit_unconfirmed_not_an_allow():
    registry = IssuerRegistry()
    registry.register(RemoteIssuerAdapter(issuer_kind="opaque", adapter_id="peer", transport=AsyncMock(),
                                         finalize_transport=AsyncMock(side_effect=TimeoutError())))
    request = IssuerRequest("actor", "request", "revoke", "card", 3, "opaque", "record", "0" * 64, "context")
    assert await registry.finalize(request, state="committed", card_revision=4) is False

"""Author host tests; not mounted human authorization or durable issuer replay qualification."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.issuer_update_host import (
    bind_issuer_update_orchestration, issuer_update_orchestration_is_bound,
)
from test_issuer_read_entrypoint import entry, module


def body():
    return {"context_ref": "opaque-intent", "request_id": "one-card-update",
        "target": {"owner_subject": "owner", "access_id": "card-one", "issuer_kind": "opaque",
            "issuer_ref": "opaque-scope", "expected_card_revision": 1, "expected_authority_fingerprint": "1" * 64},
        "delta": {"resource": "opaque-resource", "operations": ["read", "write"], "grants": ["read", "write"]}}


async def internal_call(m, e, **kwargs):
    with bind_issuer_update_orchestration():
        return await m.ConnectionHubEntrypoint.issuer_managed_card_update(e, **kwargs)


@pytest.mark.asyncio
async def test_direct_http_cannot_bind_an_update_scope(monkeypatch):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await m.ConnectionHubEntrypoint.issuer_managed_card_update(entry(m), data=body())
    assert result["error"] == "issuer_update_requires_internal_orchestration"
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["actor_subject", "tenant", "project", "candidate", "approval", "decision", "change_digest"])
async def test_query_has_no_authority_fields(monkeypatch, field):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await internal_call(m, entry(m), data={**body(), field: "forged"})
    assert result["status"] == 400
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"classification": "external"}, {"classification": "anonymous"},
    {"authority": {"authority_id": "delegated_client", "grantor_user_id": "actual-human"}},
    {"authority": {"delegated_card_binding": {"access_id": "owner-equal"}}}, {"scope": {}}])
async def test_actual_human_and_runtime_scope_are_required(monkeypatch, kwargs):
    m = module()
    factory = AsyncMock()
    monkeypatch.setattr(m, "_automation_access_service", factory)
    result = await internal_call(m, entry(m, **kwargs), data=body())
    assert result["status"] == 403
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_actor_metadata_is_ignored_and_live_host_check_is_bound(monkeypatch):
    m = module()
    e = entry(m)
    service = MagicMock()
    service.issuer_managed_card_update = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(m, "_automation_access_service", AsyncMock(return_value=service))
    result = await internal_call(m, e, data=body(), user_id="forged", fingerprint="forged")
    assert result["ok"]
    bound = service.bind_issuer_update_host.call_args.kwargs
    assert {k: v for k, v in bound.items() if k != "host_is_current"} == {
        "actor_subject": "actual-human", "actor_classification": "registered", "tenant": "actual-tenant", "project": "actual-project"}
    assert not bound["host_is_current"]()  # caller has now exited its scope


@pytest.mark.asyncio
async def test_changed_delivery_context_keeps_commit_truth_but_withholds_card(monkeypatch):
    m = module()
    e = entry(m)
    service = MagicMock()

    async def apply(*args):
        e.comm_context.user.user_id = "replacement-human"
        return {"ok": True, "state": "committed", "transaction_id": "opaque-id", "authority": {"personal": "must not escape"}}

    service.issuer_managed_card_update = AsyncMock(side_effect=apply)
    monkeypatch.setattr(m, "_automation_access_service", AsyncMock(return_value=service))
    result = await internal_call(m, e, data=body())
    assert result["state"] == "committed" and result["status"] == 202 and not result["ok"]
    assert "authority" not in result


@pytest.mark.asyncio
async def test_inherited_scope_expires_when_parent_exits():
    release = asyncio.Event()

    async def child():
        assert issuer_update_orchestration_is_bound()
        await release.wait()
        return issuer_update_orchestration_is_bound()

    with bind_issuer_update_orchestration():
        task = asyncio.create_task(child())
        await asyncio.sleep(0)
    release.set()
    assert await task is False


@pytest.mark.asyncio
async def test_update_waits_for_the_actual_sdk_card_flock_owned_by_another_process(tmp_path):
    """Real SDK/OS fence witness; cache/policy are injected, not mounted Redis."""
    import os
    import subprocess
    import sys
    from datetime import datetime, timedelta, timezone
    from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
    from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from connection_hub.delegated_credentials.issuer_update import IssuerUpdateQuery
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    original = CardAuthority(access_id="card-one", grantor_subject="owner", client_id="", delegate_subject="",
        source="control", card_kind="control", card_revision=1, issuer_kind="opaque", issuer_ref="opaque-scope",
        named_service_operations=NamedServiceSelection.none())
    raw = body()
    raw["target"]["expected_authority_fingerprint"] = original.content_hash()
    query = IssuerUpdateQuery.from_mapping(raw)
    pointer = await store.write_revision(subject_hash=query.target.subject_hash, authority=original,
                                         updated_at=datetime.now(timezone.utc))
    await store.advance_current(subject_hash=query.target.subject_hash, pointer=pointer)
    script = r'''
import asyncio, pathlib, sys
from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async
async def main():
    async with observed_file_lock_async(lock_path=pathlib.Path(sys.argv[1]), resource_id='fixture-card-one',
                                       operation='fixture-block-update', wait_seconds=5):
        print('FIXTURE_CARD_FENCE_HELD', flush=True)
        if await asyncio.to_thread(sys.stdin.readline) != 'release\n':
            raise RuntimeError('fixture release missing')
asyncio.run(main())
'''
    lock_path = store.card_path(subject_hash=query.target.subject_hash, access_id=query.target.access_id) / ".mutation.lock"
    child = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], env=os.environ.copy(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    task = None
    try:
        ready = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10)
        assert ready == "FIXTURE_CARD_FENCE_HELD\n", child.stderr.read() if not ready else ready
        cache = MagicMock()
        cache.claim_transition = AsyncMock(return_value=True)
        cache.commit_projection = AsyncMock(return_value=True)
        cache.read = AsyncMock(return_value=CardCacheEntry("updating", card_revision=1,
                                                        mutation_id=query.transaction_id("actual-human")))
        service = DelegatedCardService(store=store, cache=cache)
        service._reconcile = AsyncMock()
        service._index = AsyncMock()
        calls = []

        async def gate(original, candidate):
            calls.append(candidate.card_revision)
            return datetime.now(timezone.utc) + timedelta(seconds=20)

        task = asyncio.create_task(service.update_issuer(query, actor_subject="actual-human", before_commit=gate))
        await asyncio.sleep(0.1)
        assert not task.done() and calls == []
        child.stdin.write("release\n")
        child.stdin.flush()
        receipt = await asyncio.wait_for(task, 10)
        assert receipt["state"] == "committed" and receipt["serving_state"] == "complete"
        assert calls == [2, 2]
        assert await asyncio.wait_for(asyncio.to_thread(child.wait), 10) == 0
    finally:
        if child.poll() is None:
            child.kill()  # only the disposable fixture this test created
            await asyncio.to_thread(child.wait)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

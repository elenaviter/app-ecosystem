"""The full snapshot against the actual SDK ordered Card fences and another OS process, disposable only.

A separate process holds both production Card fences and stages a new
revision. The snapshot read must wait for both fences, make no issuer peer
call while it holds them, return the new revision of both Cards, and write
nothing.
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_read import IssuerReadQuery, IssuerReadRequest, read_digest
from connection_hub.delegated_credentials.issuer_snapshot import (
    IssuerSnapshotRegistry, RemoteIssuerSnapshotAdapter, issuer_managed_card_snapshots,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import DelegatedCardService
from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async

HOST = dict(actor_subject="human", actor_classification="registered", tenant="t", project="p")

_WRITER = r'''
import asyncio, dataclasses, json, sys
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_read import issuer_read_request_from_mapping
from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async
async def main():
    store = BundleStorageDelegatedCardStore(sys.argv[1], lifecycle_lock_scope='same-host-flock')
    request = issuer_read_request_from_mapping(json.loads(sys.argv[2]))
    async with AsyncExitStack() as stack:
        for target in sorted(request.targets, key=lambda t: (t.subject_hash, t.access_id)):
            await stack.enter_async_context(observed_file_lock_async(
                lock_path=store.card_path(subject_hash=target.subject_hash, access_id=target.access_id)/'.mutation.lock',
                resource_id=f'delegated-card:{target.access_id}', operation='fixture-pair-writer', wait_seconds=5))
        for index, target in enumerate(request.targets):
            current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
            authority = dataclasses.replace(current[1], card_revision=2)
            pointer = await store.write_revision(subject_hash=target.subject_hash, authority=authority, updated_at=datetime.now(timezone.utc))
            await store.advance_current(subject_hash=target.subject_hash, pointer=pointer)
            if index == 0:
                print('FIXTURE_FIRST_REVISION_STAGED_BOTH_FENCES_HELD', flush=True)
                if await asyncio.to_thread(sys.stdin.readline) != 'continue\n':
                    raise RuntimeError('fixture release missing')
    print('FIXTURE_BOTH_FENCES_RELEASED', flush=True)
asyncio.run(main())
'''


def _files(store):
    return {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file() and p.name != ".mutation.lock"}


async def _seed(store):
    targets = []
    for i in range(2):
        targets.append({"owner_subject": f"owner-{i}", "access_id": f"fixture-{i}",
                        "issuer_kind": "opaque", "issuer_ref": "lineage"})
    q = IssuerReadQuery.from_mapping({"context_ref": "{}", "request_id": "fixture-snapshot", "targets": targets})
    for target in q.targets:
        card = CardAuthority(access_id=target.access_id, grantor_subject=target.owner_subject, client_id="",
            delegate_subject="", source="control", card_kind="control", card_revision=1, state="active",
            issuer_kind=target.issuer_kind, issuer_ref=target.issuer_ref,
            named_service_operations=NamedServiceSelection.none())
        pointer = await store.write_revision(subject_hash=target.subject_hash, authority=card,
                                             updated_at=datetime.now(timezone.utc))
        await store.advance_current(subject_hash=target.subject_hash, pointer=pointer)
    return q


def _registry(store, q, phases):
    async def transport(payload):
        phase = payload["phase"]
        if phase == "validate":
            # The reader must have released both of its fences before this
            # peer call: taking them here must succeed at once.
            for target in sorted(q.targets, key=lambda t: (t.subject_hash, t.access_id)):
                async with observed_file_lock_async(
                        lock_path=store.card_path(subject_hash=target.subject_hash, access_id=target.access_id) / ".mutation.lock",
                        resource_id=f"delegated-card:{target.access_id}", operation="peer-fence-probe",
                        wait_seconds=0.5):
                    pass
        phases.append(phase)
        return {"ok": True, "request": payload["request"], "phase": phase,
                "snapshots_digest": read_digest(payload["snapshots"]),
                "decision": {"allowed": True, "reason": "", "policy_version": "v1",
                             "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}}

    registry = IssuerSnapshotRegistry()
    registry.register(RemoteIssuerSnapshotAdapter(issuer_kind="opaque", adapter_id="opaque", transport=transport))
    return registry


@pytest.mark.asyncio
async def test_real_fences_make_the_snapshot_wait_hold_no_peer_call_and_write_nothing(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    q = await _seed(store)
    storage = IssuerReadRequest("human", "registered", "t", "p", q.context_ref, q.request_id, q.targets)
    phases = []
    registry = _registry(store, q, phases)
    child = subprocess.Popen([sys.executable, "-c", _WRITER, str(tmp_path), json.dumps(storage.to_dict())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    reader = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10) == "FIXTURE_FIRST_REVISION_STAGED_BOTH_FENCES_HELD\n"
        cache = MagicMock()
        service = DelegatedCardService(store=store, cache=cache)
        reader = asyncio.create_task(issuer_managed_card_snapshots(q.to_dict(), registry=registry,
                                                                   persistence=service, **HOST))
        await asyncio.sleep(0.2)  # bounded adversarial ordering witness, not inbox polling
        # The first decision ran; the read now waits on the writer's fences.
        assert not reader.done() and phases == ["authorize"]
        child.stdin.write("continue\n")
        child.stdin.flush()
        result = await asyncio.wait_for(reader, 10)
        assert result["ok"], result
        assert phases == ["authorize", "validate"]
        # Never a torn pair: both Cards at the writer's committed revision.
        assert [row["card_revision"] for row in result["snapshots"]] == [2, 2]
        for row, target in zip(result["snapshots"], q.targets):
            current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
            assert row["authority"] == current[1].to_dict()
            assert row["authority_fingerprint"] == current[1].content_hash()
        assert cache.mock_calls == []
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10) == "FIXTURE_BOTH_FENCES_RELEASED\n"
        assert await asyncio.wait_for(asyncio.to_thread(child.wait), 10) == 0
    finally:
        if child.poll() is None:
            child.kill()  # ONLY the disposable fixture this test created
            await asyncio.to_thread(child.wait)
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    # With no writer, a full snapshot through the real fences writes nothing.
    before = _files(store)
    phases.clear()
    again = await issuer_managed_card_snapshots(q.to_dict(), registry=registry,
                                                persistence=DelegatedCardService(store=store, cache=MagicMock()), **HOST)
    assert again["ok"] and phases == ["authorize", "validate"]
    assert _files(store) == before

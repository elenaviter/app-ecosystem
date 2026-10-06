"""Actual SDK ordered read fences versus another OS process, disposable only."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_read import IssuerReadQuery, IssuerReadRequest
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import DelegatedCardService

_WRITER = r'''
import asyncio, dataclasses, json, pathlib, sys
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


@pytest.mark.asyncio
async def test_real_sdk_reader_waits_for_both_fences_and_cannot_return_torn_pair(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    targets = []
    for i in range(2):
        card = CardAuthority(access_id=f"fixture-{i}", grantor_subject=f"owner-{i}", client_id="",
            delegate_subject="", source="control", card_kind="control", card_revision=1, state="active",
            issuer_kind="opaque", issuer_ref="lineage", named_service_operations=NamedServiceSelection.none())
        target = {"owner_subject": card.grantor_subject, "access_id": card.access_id,
                  "issuer_kind": card.issuer_kind, "issuer_ref": card.issuer_ref}
        targets.append(target)
    q = IssuerReadQuery.from_mapping({"context_ref": "{}", "request_id": "fixture-read", "targets": targets})
    request = IssuerReadRequest("human", "registered", "t", "p", q.context_ref, q.request_id, q.targets)
    for target in request.targets:
        card = CardAuthority(access_id=target.access_id, grantor_subject=target.owner_subject, client_id="",
            delegate_subject="", source="control", card_kind="control", card_revision=1, state="active",
            issuer_kind=target.issuer_kind, issuer_ref=target.issuer_ref, named_service_operations=NamedServiceSelection.none())
        pointer = await store.write_revision(subject_hash=target.subject_hash, authority=card, updated_at=datetime.now(timezone.utc))
        await store.advance_current(subject_hash=target.subject_hash, pointer=pointer)
    child = subprocess.Popen([sys.executable, "-c", _WRITER, str(tmp_path), json.dumps(request.to_dict())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    reader = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10) == "FIXTURE_FIRST_REVISION_STAGED_BOTH_FENCES_HELD\n"
        cache = MagicMock()
        service = DelegatedCardService(store=store, cache=cache)
        reader = asyncio.create_task(service.read_lifecycle_identities(request))
        await asyncio.sleep(0.1)  # bounded adversarial ordering witness, not inbox polling
        assert not reader.done()
        child.stdin.write("continue\n")
        child.stdin.flush()
        cards = await asyncio.wait_for(reader, 10)
        assert [c.card_revision for c in cards] == [2, 2]
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

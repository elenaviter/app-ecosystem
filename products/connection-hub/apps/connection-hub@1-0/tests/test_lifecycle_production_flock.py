"""Real SDK flock held by a disposable other process beyond the 30s deadline.

No runtime client/relay/credentials/production Card state is touched.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.cards.lifecycle_store import read_receipt
from connection_hub.delegated_credentials.issuer_gate import change_digest
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import (
    CardConflict, DelegatedCardService,
)

_HOLDER = r'''
import asyncio, pathlib, sys
from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async
async def main():
    async with observed_file_lock_async(lock_path=pathlib.Path(sys.argv[1]), resource_id=sys.argv[2],
            operation='delegated-card-mutation', wait_seconds=5):
        print('FIXTURE_FLOCK_HELD', flush=True)
        await asyncio.sleep(32)
asyncio.run(main())
'''


@pytest.mark.asyncio
async def test_sdk_flock_holder_past_real_30_second_deadline_has_no_write_and_releases_waiter_fences(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = [CardAuthority(access_id=f"fixture-card-{i}", client_id="", delegate_subject="", grantor_subject=f"owner-{i}",
        source="control", card_kind="control", card_revision=1, state="active", issuer_kind=f"opaque-{i}",
        issuer_ref="opaque", named_service_operations=NamedServiceSelection.none()) for i in range(2)]
    targets = []
    for card in cards:
        import hashlib
        scope = hashlib.sha256(card.grantor_subject.encode()).hexdigest()
        pointer = await store.write_revision(subject_hash=scope, authority=card, updated_at=datetime.now(timezone.utc))
        await store.advance_current(subject_hash=scope, pointer=pointer)
        targets.append({"owner_subject": card.grantor_subject, "access_id": card.access_id,
            "expected_card_revision": 1, "expected_authority_fingerprint": card.content_hash(),
            "issuer_kind": card.issuer_kind, "issuer_ref": card.issuer_ref})
    targets.sort(key=lambda t: (t["owner_subject"], t["access_id"]))
    request = LifecycleRequest.from_mapping({"context_ref": "fixture-context", "request_id": "fixture-request", "action": "revoke",
        "change_digest": change_digest({"action": "revoke", "targets": targets}), "targets": targets})
    first = min(request.targets, key=lambda t: (t.subject_hash, t.access_id))
    lock_path = store.card_path(subject_hash=first.subject_hash, access_id=first.access_id) / ".mutation.lock"
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock_path), f"delegated-card:{first.access_id}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10) == "FIXTURE_FLOCK_HELD\n"
        cache = MagicMock()
        cache.claim_lifecycle = AsyncMock()
        cache.reconcile_projection = AsyncMock()
        service = DelegatedCardService(store=store, cache=cache)
        gate, cleanup = AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=60)), AsyncMock()
        start = time.monotonic()
        with pytest.raises(CardConflict, match="card_lifecycle_timeout"):
            await service.revoke_lifecycle(request, actor_subject="fixture-human", before_commit=gate, after_commit=cleanup)
        elapsed = time.monotonic() - start
        assert 29 <= elapsed < 36
        assert child.poll() is None  # real other holder still owns its flock
        gate.assert_not_called()
        cleanup.assert_not_called()
        cache.claim_lifecycle.assert_not_called()
        cache.reconcile_projection.assert_not_called()
        assert await read_receipt(store, request.transaction_id("fixture-human")) is None
        for target in request.targets:
            current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
            assert (current[1].state, current[1].card_revision) == ("active", 1)
        from kdcube_ai_app.storage.observed_file_locks import observed_file_lock_async
        receipt_lock = store.root / "lifecycle-transactions" / f"{request.transaction_id('fixture-human')}.lock"
        async with observed_file_lock_async(lock_path=receipt_lock, resource_id="fixture-proof", operation="fixture", wait_seconds=1):
            pass  # the timed-out waiter released its acquired receipt fence
        child.communicate(timeout=6)
        assert child.returncode == 0
        print(json.dumps({"fixture": "sdk-flock-deadline", "elapsed_seconds": round(elapsed, 3),
            "commits": 0, "current_revisions": [1, 1], "receipt_fence_reacquired": True}))
    finally:
        if child.poll() is None:
            child.terminate()  # ONLY our disposable fixture process
            child.communicate(timeout=5)

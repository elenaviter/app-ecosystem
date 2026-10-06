"""W569: the sealed two-Card read against a really interrupted pair revoke.

A fixture-owned child running the hosted pair revoke is SIGKILLed mid-write,
over real BundleStorage and Redis (``REDIS_URL``). The protected identity read
then runs through ``AutomationAccessService.issuer_managed_lifecycle_read`` on
the hosted KDCube persistence, with real ``observed_file_lock_async`` fences:

- while the pair is unresolved, it refuses retryably (409), writes and repairs
  nothing, and returns no snapshot;
- after the identical request recovers the pair, it returns both identities.
  Each snapshot's revision and full fingerprint equal the committed Card on
  storage, and both issuers' second check is bound to exactly those snapshots.

Only the issuers' peer transport is the test's own: it records each phase and
answers allowed with the exact echo the protocol requires.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.issuer_read import (
    IssuerReadQuery, IssuerReadRegistry, RemoteIssuerReadAdapter, read_digest,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from test_w569_lifecycle_real_redis import ACTOR, _pair, _seed

CHILD = Path(__file__).with_name("_w569_service_kill_child.py")

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="REDIS_URL is not set; real-Redis read is skipped")


def _child(request: dict, *, expect_kill: bool = False) -> dict:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join([str(CHILD.parent), os.environ.get("PYTHONPATH", "")])}
    done = subprocess.run([sys.executable, str(CHILD)], input=json.dumps(request), env=environment,
                          capture_output=True, text=True, timeout=60)
    if expect_kill:
        assert done.returncode == -signal.SIGKILL, done.stderr[-2000:]
        return {}
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def _query(cards):
    return IssuerReadQuery.from_mapping({"context_ref": "{\"purpose\":\"w569-read\"}", "request_id": "read-1",
        "targets": [{"owner_subject": c.grantor_subject, "access_id": c.access_id,
                     "issuer_kind": c.issuer_kind, "issuer_ref": c.issuer_ref} for c in cards]})


def _port(root, cards, calls):
    import redis.asyncio as redis_asyncio

    async def transport(payload):
        calls.append({"phase": payload["phase"], "snapshots": payload["snapshots"]})
        return {"ok": True, "request": payload["request"], "phase": payload["phase"],
                "snapshots_digest": read_digest(payload["snapshots"]),
                "decision": {"allowed": True, "reason": "", "policy_version": "v1",
                             "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}}

    registry = IssuerReadRegistry()
    for card in cards:
        registry.register(RemoteIssuerReadAdapter(issuer_kind=card.issuer_kind, adapter_id=card.issuer_kind,
                                                  transport=transport))
    store = BundleStorageDelegatedCardStore(root, lifecycle_lock_scope="same-host-flock")
    client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    persistence = DurableCardPersistence(redis=client, tenant="read-t", project="read-p", card_store=store)
    port = AutomationAccessService.__new__(AutomationAccessService)
    port._persistence = persistence
    port.bind_issuer_read_registry(registry, actor_subject=ACTOR, actor_classification="registered",
                                   tenant="tenant", project="hosting-project")
    return port, store, client


def _files(root):
    return {p: p.read_bytes() for p in Path(root).rglob("*") if p.is_file() and not p.name.endswith(".lock")}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pointer1", "committed"])
async def test_the_sealed_read_refuses_an_interrupted_pair_without_repair_then_reads_the_committed_pair(tmp_path, stage):
    cards = _pair()
    await _seed(BundleStorageDelegatedCardStore(tmp_path), cards)
    base = {"storage_root": str(tmp_path), "redis_url": os.environ["REDIS_URL"],
            "tenant": f"t-{uuid.uuid4().hex[:8]}", "project": f"p-{uuid.uuid4().hex[:8]}"}
    _child({**base, "action": "run", "stage": stage}, expect_kill=True)
    calls = []
    port, store, client = _port(tmp_path, cards, calls)
    try:
        before = _files(tmp_path)

        pending = await port.issuer_managed_lifecycle_read(_query(cards).to_dict())

        assert pending == {"ok": False, "status": 409, "error": "issuer_read_lifecycle_pending", "retryable": True}
        assert _files(tmp_path) == before, "the read writes and repairs nothing"
        assert [call["phase"] for call in calls] == ["authorize", "authorize"], "no second check without a snapshot"

        recovered = _child({**base, "action": "recover"})
        calls.clear()
        read = await port.issuer_managed_lifecycle_read(_query(cards).to_dict())
    finally:
        await client.aclose()

    assert read["ok"] is True, read
    expected = [["revoked", 2], ["revoked", 2]] if stage == "committed" else [["active", 1], ["active", 1]]
    assert recovered["durable"] == expected
    for row, card in zip(read["snapshots"], cards):
        current = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                     access_id=card.access_id)
        assert (row["card_revision"], row["authority_fingerprint"]) == (current[1].card_revision, current[1].content_hash())
    validates = [call for call in calls if call["phase"] == "validate"]
    assert len(validates) == 2 and all(call["snapshots"] == read["snapshots"] for call in validates)

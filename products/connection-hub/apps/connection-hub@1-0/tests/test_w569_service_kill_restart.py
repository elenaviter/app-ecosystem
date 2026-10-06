"""W569: kill the hosted pair revoke at each durable boundary, restart, recover.

The existing kill tests stop ``lifecycle_store.atomic_revoke`` alone, with no
Card fences and no Redis. These kill the hosted service itself: KDCube
``DurableCardPersistence`` with real ``observed_file_lock_async`` fences, over
real BundleStorage and a real Redis (``REDIS_URL``). Each case uses a fresh
child process for each step, so no result can come from the killed process's
memory, open lock or pending task:

1. the child is SIGKILLed at one boundary: intent published, Redis pair
   claimed, first pointer, second pointer, committed receipt, or cleanup;
2. a fresh process finds both Cards' ordinary writers refused while the pair
   is unresolved;
3. a fresh process sends the identical request, and the pair resolves under
   the fences: before the visibility rename it is refused with both Cards
   active; after it, it is committed with both Cards revoked. In both cases it
   is serving-complete, Redis holds no unresolved marker, and the killed
   process's fences do not hang recovery;
4. the ordinary writers work again (after a refusal), and recovery is idempotent.

Only these fixture-owned children are killed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from test_w569_lifecycle_real_redis import _pair, _seed

CHILD = Path(__file__).with_name("_w569_service_kill_child.py")
BEFORE_VISIBILITY = ("intent", "claimed", "pointer1", "pointer2")
AFTER_VISIBILITY = ("committed", "cleanup")

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="REDIS_URL is not set; real-Redis restart is skipped")


def _child(request: dict, *, expect_kill: bool = False) -> dict:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join([str(CHILD.parent), os.environ.get("PYTHONPATH", "")])}
    done = subprocess.run([sys.executable, str(CHILD)], input=json.dumps(request), env=environment,
                          capture_output=True, text=True, timeout=60)
    if expect_kill:
        assert done.returncode == -signal.SIGKILL, (done.returncode, done.stdout, done.stderr[-2000:])
        return {}
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", BEFORE_VISIBILITY + AFTER_VISIBILITY)
async def test_a_killed_hosted_pair_revoke_blocks_both_writers_and_recovers_once_in_a_fresh_process(tmp_path, stage):
    await _seed(BundleStorageDelegatedCardStore(tmp_path), _pair())
    base = {"storage_root": str(tmp_path), "redis_url": os.environ["REDIS_URL"],
            "tenant": f"t-{uuid.uuid4().hex[:8]}", "project": f"p-{uuid.uuid4().hex[:8]}"}

    _child({**base, "action": "run", "stage": stage}, expect_kill=True)

    blocked = _child({**base, "action": "single"})
    assert blocked["single"] == ["lifecycle_preparation_unresolved"] * 2, blocked

    started = time.monotonic()
    recovered = _child({**base, "action": "recover"})
    took = time.monotonic() - started
    assert took < 30, f"recovery waited on the killed process's fences: {took:.1f}s"
    assert recovered["serving_state"] == "complete", recovered
    if stage in AFTER_VISIBILITY:
        assert recovered["state"] == "committed", recovered
        assert recovered["durable"] == [["revoked", 2], ["revoked", 2]], recovered
        assert recovered["served"] == [["revoked", 2], ["revoked", 2]], recovered
    else:
        assert recovered["state"] == "refused", recovered
        assert recovered["durable"] == [["active", 1], ["active", 1]], recovered
        assert all(value is None or value[0] == "card" for value in recovered["served"]), recovered

    again = _child({**base, "action": "recover"})
    assert again == recovered, "recovery is idempotent"
    if stage in BEFORE_VISIBILITY:
        assert _child({**base, "action": "single"})["single"] == ["committed", "committed"]

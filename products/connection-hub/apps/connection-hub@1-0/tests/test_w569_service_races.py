"""W569: concurrent hosted pair revokes and single-Card writers, in separate processes.

Each racer is a fresh process running the hosted composition: KDCube
``DurableCardPersistence`` with real ``observed_file_lock_async`` fences, over one
real BundleStorage root and a real Redis (``REDIS_URL``). The holder signals
from inside its issuer decision, while it holds the receipt fence and both
ordered Card fences, then waits. The competitor starts only after that signal,
so it really meets held fences:

- the same pair named in the opposite order, and overlapping pairs sharing a
  Card: no deadlock, and exactly one revoke takes each shared Card;
- a pair against a single-Card writer: the single write never interleaves.
  Either the pair commits whole and the writer is refused, or the writer
  commits first and the pair refuses its moved target. Never half.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for

sys.path.insert(0, str(Path(__file__).parent))
from _w569_service_kill_child import race_card  # noqa: E402
from test_w569_lifecycle_real_redis import MOMENT  # noqa: E402

CHILD = Path(__file__).with_name("_w569_service_kill_child.py")
HOLD = 2.0

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="REDIS_URL is not set; real-Redis races are skipped")


def _start(request: dict) -> subprocess.Popen:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join([str(CHILD.parent), os.environ.get("PYTHONPATH", "")])}
    process = subprocess.Popen([sys.executable, str(CHILD)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment, text=True)
    process.stdin.write(json.dumps(request))
    process.stdin.close()
    return process


def _result(process: subprocess.Popen) -> dict:
    process.wait(timeout=90)
    out, err = process.stdout.read(), process.stderr.read()
    assert process.returncode == 0, err[-2000:]
    return json.loads(out)


async def _seed(root, indexes):
    store = BundleStorageDelegatedCardStore(root)
    for index in indexes:
        card = race_card(index)
        scope = subject_hash_for(card.grantor_subject)
        pointer = await store.write_revision(subject_hash=scope, authority=card, updated_at=MOMENT)
        await store.advance_current(subject_hash=scope, pointer=pointer)
    return store


async def _durable(store, indexes):
    states = []
    for index in indexes:
        card = race_card(index)
        loaded = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                    access_id=card.access_id)
        states.append([loaded[1].state, loaded[1].card_revision])
    return states


def _race(tmp_path, holder: dict, competitor: dict):
    base = {"storage_root": str(tmp_path / "storage"), "redis_url": os.environ["REDIS_URL"],
            "tenant": f"t-{uuid.uuid4().hex[:8]}", "project": f"p-{uuid.uuid4().hex[:8]}"}
    inside = tmp_path / "holder-inside"
    first = _start({**base, **holder, "hold": HOLD, "inside": str(inside)})
    deadline = time.monotonic() + 30
    while not inside.exists():
        assert first.poll() is None, first.stderr.read()[-2000:]
        assert time.monotonic() < deadline, "holder never reached its fences"
        time.sleep(0.02)
    second = _start({**base, **competitor})
    return _result(first), _result(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_order", [[0, 1], [1, 0]], ids=["same-order", "reverse-order"])
async def test_two_pairs_on_the_same_cards_never_deadlock_and_exactly_one_revokes(tmp_path, second_order):
    store = await _seed(tmp_path / "storage", [0, 1])

    first, second = _race(tmp_path, {"action": "pair", "cards": [0, 1], "request_id": "pair-a"},
                          {"action": "pair", "cards": second_order, "request_id": "pair-b"})

    assert first["state"] == "committed", first
    assert second["state"] != "committed", second
    # The competitor waited for the held fences instead of interleaving.
    assert second["seconds"] >= HOLD * 0.5, second
    assert await _durable(store, [0, 1]) == [["revoked", 2], ["revoked", 2]]


@pytest.mark.asyncio
async def test_overlapping_pairs_sharing_one_card_never_deadlock_and_never_half_apply(tmp_path):
    store = await _seed(tmp_path / "storage", [0, 1, 2])

    first, second = _race(tmp_path, {"action": "pair", "cards": [0, 1], "request_id": "pair-a"},
                          {"action": "pair", "cards": [2, 1], "request_id": "pair-b"})

    assert first["state"] == "committed", first
    assert second["state"] != "committed", second
    # The losing pair touched neither of its Cards, including the unshared one.
    assert await _durable(store, [0, 1, 2]) == [["revoked", 2], ["revoked", 2], ["active", 1]]


@pytest.mark.asyncio
async def test_a_single_card_writer_never_interleaves_with_a_held_pair(tmp_path):
    store = await _seed(tmp_path / "storage", [0, 1])

    first, second = _race(tmp_path, {"action": "pair", "cards": [0, 1], "request_id": "pair-a"},
                          {"action": "single_card", "cards": [0]})

    assert first["state"] == "committed", first
    assert second["state"] == "raised", second
    assert await _durable(store, [0, 1]) == [["revoked", 2], ["revoked", 2]]


@pytest.mark.asyncio
async def test_a_held_single_card_writer_makes_the_pair_refuse_its_moved_target_whole(tmp_path):
    store = await _seed(tmp_path / "storage", [0, 1])
    base = {"storage_root": str(tmp_path / "storage"), "redis_url": os.environ["REDIS_URL"],
            "tenant": f"t-{uuid.uuid4().hex[:8]}", "project": f"p-{uuid.uuid4().hex[:8]}"}

    single = _result(_start({**base, "action": "single_card", "cards": [0]}))
    pair = _result(_start({**base, "action": "pair", "cards": [0, 1], "request_id": "pair-after"}))

    assert single["state"] == "committed", single
    assert pair["state"] != "committed", pair
    # The pair recorded card 0 at revision 1; its fingerprint no longer matches, so neither Card changes.
    assert await _durable(store, [0, 1]) == [["active", 2], ["active", 1]]

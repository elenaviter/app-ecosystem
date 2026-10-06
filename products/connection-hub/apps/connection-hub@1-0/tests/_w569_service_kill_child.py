"""Fresh-process child for the W569 hosted-service kill/restart regressions.

Every action builds the hosted composition anew: the KDCube
``DurableCardPersistence`` (real ``observed_file_lock_async`` fences) over a
real ``BundleStorageDelegatedCardStore`` and the real Redis at ``redis_url``.
``run`` kills this process at one named boundary of the pair revoke; nothing
else is replaced. ``recover`` sends the identical request. ``single`` tries
each Card's ordinary single-Card writer. ``snapshot`` reads durable and Redis
state. The parent passes only synthetic coordinates and a temporary root.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis_asyncio

from connection_hub.delegated_credentials.cards import lifecycle_store
from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.credential_handles import RedisCardCredentialHandleStore
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from test_w569_lifecycle_real_redis import ACTOR, _pair, _request


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _install_kill(stage: str, store) -> None:
    if stage == "claimed":
        original_claim = DelegatedCardRuntimeCache.claim_lifecycle

        async def claim_then_die(self, *args, **kwargs):
            result = await original_claim(self, *args, **kwargs)
            _die()
            return result

        DelegatedCardRuntimeCache.claim_lifecycle = claim_then_die
        return
    if stage == "cleanup":
        original_remove = RedisCardCredentialHandleStore.remove

        async def remove_then_die(self, *args, **kwargs):
            await original_remove(self, *args, **kwargs)
            _die()

        RedisCardCredentialHandleStore.remove = remove_then_die
        return
    original_write = lifecycle_store.write_json_atomic
    pointers = 0

    async def write_then_die(path, payload):
        nonlocal pointers
        await original_write(path, payload)
        schema = payload.get("schema") if isinstance(payload, dict) else None
        if stage == "intent" and path == lifecycle_store.active_intent_path(store, payload.get("transaction_id", "")):
            _die()
        if schema == lifecycle_store.LIFECYCLE_POINTER_SCHEMA:
            pointers += 1
            if stage == f"pointer{pointers}":
                _die()
        if (stage == "committed" and schema == lifecycle_store.LIFECYCLE_RECEIPT_SCHEMA
                and payload.get("state") == "committed"):
            _die()

    lifecycle_store.write_json_atomic = write_then_die


async def _decision(_authorities):
    return datetime.now(timezone.utc) + timedelta(seconds=30)


async def _snapshot(store, client, cache, cards) -> dict:
    durable, served = [], []
    for card in cards:
        loaded = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                    access_id=card.access_id)
        durable.append(None if loaded is None else [loaded[1].state, loaded[1].card_revision])
        raw = await client.get(cache.card_key(card.access_id))
        value = None if raw is None else json.loads(raw)
        served.append(None if value is None else [value.get("kind"), value.get("card_revision")])
    return {"durable": durable, "served": served}


async def main(request: dict) -> dict:
    client = redis_asyncio.from_url(request["redis_url"])
    store = BundleStorageDelegatedCardStore(request["storage_root"], lifecycle_lock_scope="same-host-flock")
    persistence = DurableCardPersistence(redis=client, tenant=request["tenant"], project=request["project"],
                                         card_store=store)
    cache = DelegatedCardRuntimeCache(client, tenant=request["tenant"], project=request["project"])
    cards = _pair()
    lifecycle_request = _request(cards)
    action = request["action"]
    try:
        if action == "run":
            _install_kill(request["stage"], store)
            receipt = await persistence.revoke_lifecycle(lifecycle_request, actor_subject=ACTOR, before_commit=_decision)
            return {"survived": True, "state": receipt["state"]}
        if action == "recover":
            receipt = await persistence.revoke_lifecycle(lifecycle_request, actor_subject=ACTOR, before_commit=_decision)
            return {"state": receipt["state"], "serving_state": receipt["serving_state"],
                    **await _snapshot(store, client, cache, cards)}
        if action == "single":
            outcomes = []
            for card in cards:
                current = await persistence.current_revision(card.access_id,
                                                             subject_hash=subject_hash_for(card.grantor_subject))
                try:
                    await persistence._cards.commit(dataclasses.replace(card, card_revision=current + 1),
                        subject_hash=subject_hash_for(card.grantor_subject), expected_revision=current)
                    outcomes.append("committed")
                except Exception as exc:  # the refusal reason is the evidence
                    outcomes.append(getattr(exc, "reason", type(exc).__name__))
            return {"single": outcomes}
        if action == "snapshot":
            return await _snapshot(store, client, cache, cards)
        raise ValueError("child_action_unknown")
    finally:
        await client.aclose()


if __name__ == "__main__":
    result = asyncio.run(main(json.load(sys.stdin)))
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")

"""Disposable UPDATE witness child; real storage, SDK fences, Redis and index.

Only this process is killed. The issuer adapter and host-current predicate are
synthetic; no mounted request, external policy, credential or service is used.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from connection_hub.delegated_credentials.cards import update_store
from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from connection_hub.delegated_credentials.issuer_update import IssuerUpdateQuery, issuer_managed_card_update
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence

ACTOR = "fixture-authenticated-human"
STAGES = ("intent", "marker", "sidecar", "revision", "pointer", "committed", "projection", "complete")


def validate_fixture(backend, target, confirmed):
    """An explicit dedicated fixture, loopback only, without credential input.

    This guard does not prove a server is disposable. Its owner must identify
    that fixture separately; the tests never discover or provision a server.
    The conventional deployed Redis port is deliberately refused.
    """
    if confirmed != "1":
        raise ValueError("disposable_fixture_confirmation_required")
    if backend not in ("standalone", "cluster"):
        raise ValueError("fixture_backend_invalid")
    try:
        parsed = urlsplit(target if backend == "standalone" else "redis://" + target)
        port = parsed.port
        valid = (parsed.scheme == "redis" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                 and parsed.username is None and parsed.password is None
                 and port is not None and 1 <= port <= 65535 and port != 6379
                 and not parsed.query and not parsed.fragment)
        if backend == "standalone":
            valid = valid and (parsed.path in ("", "/") or re.fullmatch(r"/[0-9]+", parsed.path) is not None)
        else:
            valid = valid and parsed.path == ""
    except (TypeError, ValueError):
        valid = False
    if not valid:
        # Never include a possibly credential-bearing target in an exception.
        raise ValueError("dedicated_loopback_fixture_target_required")
    return parsed.hostname, port


def original_card():
    return CardAuthority(
        access_id="update-fixture-card", grantor_subject="update-fixture-owner", source="control",
        client_id="", delegate_subject="", card_kind="control", card_revision=1, state="active",
        label="Original fixture", issuer_kind="opaque-fixture", issuer_ref="fixture-lineage",
        resource_operations={"selected": ("read",), "preserved": ("other",)},
        resource_grants={"selected": ("read",), "preserved": ("other-grant",)},
        named_service_operations=NamedServiceSelection.none(),
        provenance={"fixture": {"unchanged": True}}, properties={"opaque": {"unchanged": True}},
        created_at=100, last_issued_at=200)


def update_wire():
    card = original_card()
    return {"context_ref": "fixture-immutable-intent", "request_id": "fixture-update-request",
            "target": {"owner_subject": card.grantor_subject, "access_id": card.access_id,
                       "issuer_kind": card.issuer_kind, "issuer_ref": card.issuer_ref,
                       "expected_card_revision": card.card_revision,
                       "expected_authority_fingerprint": card.content_hash()},
            "delta": {"resource": "selected", "operations": ["read", "write"], "grants": ["read", "write"]}}


def _die():
    os.kill(os.getpid(), signal.SIGKILL)


def _install_kill(stage, store, query):
    if stage not in STAGES:
        raise ValueError("fixture_crash_stage_invalid")
    write = update_store.write_json_atomic
    transaction = query.transaction_id(ACTOR)

    async def write_then_die(path, payload):
        await write(path, payload)
        if stage == "intent" and path == update_store.active_path(store, transaction):
            _die()
        if stage == "sidecar" and path.name.endswith(".issuer-update.json"):
            _die()
        if stage == "pointer" and payload.get("schema") == update_store.UPDATE_POINTER_SCHEMA:
            _die()
        if path == update_store.receipt_path(store, transaction) and payload.get("state") == "committed":
            if stage == "committed" and payload["serving_state"] == "pending":
                _die()
            if stage == "complete" and payload["serving_state"] == "complete":
                _die()

    update_store.write_json_atomic = write_then_die
    if stage == "revision":
        write_revision = store.write_revision

        async def revision_then_die(**kwargs):
            result = await write_revision(**kwargs)
            _die()
            return result

        store.write_revision = revision_then_die
    if stage == "marker":
        claim = DelegatedCardRuntimeCache.claim_transition

        async def claim_then_die(self, *args, **kwargs):
            result = await claim(self, *args, **kwargs)
            if result:
                _die()
            return result

        DelegatedCardRuntimeCache.claim_transition = claim_then_die
    if stage == "projection":
        install = DelegatedCardRuntimeCache.commit_projection

        async def projection_then_die(self, *args, **kwargs):
            result = await install(self, *args, **kwargs)
            if result:
                _die()
            return result

        DelegatedCardRuntimeCache.commit_projection = projection_then_die


async def main(request):
    host, port = validate_fixture(request["backend"], request["target"], request["confirmed"])
    if re.fullmatch(r"issuer-test-[0-9a-f]{32}", request["tenant"]) is None:
        raise ValueError("fixture_namespace_required")
    root = Path(request["storage_root"])
    if root.name != "state" or (root / ".fixture-owned").read_text() != "issuer-update-test\n":
        raise ValueError("fixture_owned_storage_required")
    if request["backend"] == "cluster":
        from redis.asyncio.cluster import RedisCluster
        client = RedisCluster(host=host, port=port, socket_timeout=5, socket_connect_timeout=5)
        await client.initialize()
    else:
        import redis.asyncio as redis_asyncio
        client = redis_asyncio.from_url(request["target"], socket_timeout=5, socket_connect_timeout=5)
    try:
        await client.ping()
        store = BundleStorageDelegatedCardStore(root, lifecycle_lock_scope="same-host-flock")
        persistence = DurableCardPersistence(redis=client, tenant=request["tenant"],
            project="issuer-update-recovery", card_store=store)
        cache = persistence._cards._cache
        original = original_card()
        query = IssuerUpdateQuery.from_mapping(update_wire())
        transaction = query.transaction_id(ACTOR)
        key = cache.card_key(original.access_id)
        index_key = cache.grantor_index_key(query.target.subject_hash)

        async def snapshot():
            current = await store.read_current_authority(subject_hash=query.target.subject_hash,
                                                        access_id=original.access_id)
            entry = await cache.read(original.access_id)
            raw_members = await client.zrange(index_key, 0, -1)
            return {"revision": current[1].card_revision, "fingerprint": current[1].content_hash(),
                    "label": current[1].label,
                    "history": len(await store.list_revision_names(subject_hash=query.target.subject_hash,
                                                                    access_id=original.access_id)),
                    "active": update_store.active_path(store, transaction).exists(),
                    "cache_kind": None if entry is None else entry.kind,
                    "cache_revision": None if entry is None else entry.card_revision,
                    "cache_mutation": None if entry is None else entry.mutation_id,
                    "cache_fingerprint": None if entry is None or entry.authority is None else entry.authority.content_hash(),
                    "index_members": [v.decode() if isinstance(v, bytes) else v for v in raw_members]}

        action = request["action"]
        if action == "cleanup":
            # Cluster keys may be in different slots. Delete only each exact
            # key this unique fixture namespace owns; never SCAN or FLUSH.
            keys = (key, index_key, cache.projection_epoch_key(), cache.reconcile_lock_key())
            for owned_key in keys:
                await client.delete(owned_key)
            return {"remaining": sum([await client.exists(k) for k in keys])}
        if action == "seed":
            await persistence._cards.commit(original, subject_hash=query.target.subject_hash, expected_revision=0)
            return await snapshot()
        if action == "snapshot":
            return await snapshot()
        if action == "single":
            current = await store.read_current_authority(subject_hash=query.target.subject_hash,
                                                        access_id=original.access_id)
            later = dataclasses.replace(current[1], card_revision=current[1].card_revision + 1,
                                        label="Later legitimate fixture edit")
            try:
                await persistence._cards.commit(later, subject_hash=query.target.subject_hash,
                                                expected_revision=current[1].card_revision)
                return {"single": "committed"}
            except Exception as exc:
                return {"single": getattr(exc, "reason", type(exc).__name__)}
        if action in ("foreign_marker", "owned_marker"):
            value = {"kind": "updating", "card_revision": 1,
                     "mutation_id": "foreign-fixture-mutation" if action == "foreign_marker" else transaction}
            await client.set(key, json.dumps(value, sort_keys=True))
            return await snapshot()
        if action == "drop_projection":
            await client.delete(key)
            await client.delete(index_key)
            return await snapshot()
        if action == "expire_and_read_through":
            assert await client.expire(key, 1)
            until = time.monotonic() + 5
            while await client.exists(key) and time.monotonic() < until:
                await asyncio.sleep(0.05)
            assert not await client.exists(key), "fixture marker did not expire"
            # The real resolver's miss/read-through component, not an injected
            # cache refill. Full mounted resolution remains a separate gate.
            restored = await persistence._resolver._restore(subject_hash=query.target.subject_hash,
                access_id=original.access_id, moment=int(time.time()))
            assert restored == original
            return await snapshot()

        decisions, finalizations = [], []

        class FixtureIssuer:
            issuer_kind = original.issuer_kind
            adapter_id = "fixture-sealed-issuer"

            async def decide(self, issuer_request):
                assert issuer_request.actor_subject == ACTOR and issuer_request.action == "update"
                decisions.append(issuer_request)
                return True, "", "fixture-policy-v1", datetime.now(timezone.utc) + timedelta(seconds=30)

            async def finalize_context(self, issuer_request, *, outcome):
                finalizations.append(outcome)
                return True

        registry = IssuerRegistry()
        registry.register(FixtureIssuer())
        raw = update_wire()
        if action == "run":
            _install_kill(request["stage"], store, query)
        elif action == "changed_replay":
            raw["delta"]["operations"] = ["admin", "read", "write"]
        elif action != "recover":
            raise ValueError("fixture_child_action_invalid")
        result = await issuer_managed_card_update(raw, actor_subject=ACTOR, registry=registry,
            persistence=persistence, host_is_current=lambda: True)
        after = result.get("authority")
        return {"state": result.get("state"), "serving_state": result.get("serving_state"),
                "status": result["status"], "error": result.get("error"),
                "decisions": len(decisions), "finalizations": len(finalizations),
                "after_revision": None if after is None else after["card_revision"],
                "after_fingerprint": result.get("authority_fingerprint"), **await snapshot()}
    finally:
        await client.aclose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.load(sys.stdin))), sort_keys=True))

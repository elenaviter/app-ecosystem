"""Independent exact-P6e public-store cut after the REAL R owner guard, before real SQL CAS.

Uses the author's actual SDK-R/observed-flock/real-PG world. No fake ownership,
Redis DEL, SQL edits, replacement SQL/result, assertion weakening or body swap.
The pause is only scheduling at the real _fetchrow boundary after the actual
production _guard_row_write completed. Two owned local roots model containers.
Not installed-runtime or full PB E2E proof. The no-stall control must pass.


Retained on the P fix head (Spark), unchanged: it fails on 6e0320df (A's CAS lands after its real owner check,
B is refused) and passes once a locked read claims the row (fence) and every CAS requires that fence.
"""
import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
                                reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")

from connection_hub.delegated_credentials.cards import store as store_module
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_w661_p_row_write_owner_guard import TXN, world

W661_OWN_R_LOCK = True


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_after_guard", [False, True])
async def test_successor_keeps_authority_when_old_sql_resumes_after_real_guard(
        tmp_path, monkeypatch, stall_after_guard):
    async with world(tmp_path) as (store, rows, redis, key, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN,
                  "state": "staged", "members": []}
        async with held(0):
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)

        at_sql, resume_sql, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = {"stall_after_guard": stall_after_guard}
        actual_guard = store_module._guard_row_write
        actual_fetchrow = rows._fetchrow

        async def record_actual_guard(path):
            await actual_guard(path)  # always the real proc and R owner guards; no replacement
            if asyncio.current_task().get_name() == "independent-old-A":
                observed["a_real_owner_guard_passed"] = True

        async def schedule_actual_sql(query, *args):
            old_update = (query.lstrip().startswith("UPDATE")
                          and "connection_hub_card_version_txns" in query
                          and len(args) >= 4 and isinstance(args[3], bytes)
                          and json.loads(args[3]).get("by") == "A")
            if old_update:
                assert observed.get("a_real_owner_guard_passed") is True
                observed["a_key_present_after_real_guard"] = bool(await redis.get(key))
                assert observed["a_key_present_after_real_guard"] is True
                at_sql.set()
                if stall_after_guard:
                    await asyncio.wait_for(resume_sql.wait(), 8)
                result = await actual_fetchrow(query, *args)  # unchanged actual conditional SQL and result
                observed["a_cas_row_revision"] = None if result is None else int(result["row_revision"])
                return result
            return await actual_fetchrow(query, *args)

        monkeypatch.setattr(store_module, "_guard_row_write", record_actual_guard)
        monkeypatch.setattr(rows, "_fetchrow", schedule_actual_sql)

        async def old_a():
            try:
                async with held(0):
                    assert await store.read_marker_document(TXN) == staged
                    try:
                        await store.write_marker_document(TXN, {**staged, "state": "published", "by": "A"})
                        observed["a_result"] = "written"
                    except CardStorageError as exc:
                        observed["a_result"] = str(exc)
            finally:
                a_done.set()

        async def successor_b():
            await asyncio.wait_for(at_sql.wait(), 5)
            if stall_after_guard:
                async def expires():
                    while await redis.exists(key):
                        await asyncio.sleep(.02)
                await asyncio.wait_for(expires(), 4)  # observed real expiry, no DEL or clock substitution
            else:
                await asyncio.wait_for(a_done.wait(), 8)
            async with held(1):
                before = await store.read_marker_document(TXN)
                if stall_after_guard:
                    assert before == staged  # B observes the same revision A decided on
                else:
                    assert before["by"] == "A"
                observed["b_initial_revision"] = (await rows.read_marker(TXN))[1]
                token = await redis.get(key)
                observed["b_key_present_before_old_sql"] = bool(token)
                assert token
                resume_sql.set()
                await asyncio.wait_for(a_done.wait(), 8)
                observed["b_key_survived_a_release"] = (await redis.get(key)) == token
                try:
                    await store.write_marker_document(TXN, {**staged, "state": "published", "by": "B"})
                    observed["b_result"] = "written"
                except CardStorageError as exc:
                    observed["b_result"] = str(exc)
                observed["b_key_unchanged_after_cas"] = (await redis.get(key)) == token

        a = asyncio.create_task(old_a(), name="independent-old-A")
        b = asyncio.create_task(successor_b(), name="independent-successor-B")
        try:
            await asyncio.wait_for(asyncio.gather(a, b), 20)
        finally:
            resume_sql.set()
            await asyncio.gather(a, b, return_exceptions=True)

        final, revision = await rows.read_marker(TXN)
        observed["final_by"], observed["final_revision"] = final.get("by"), revision
        print("independent real guard-to-CAS cut:", json.dumps(observed, sort_keys=True))
        # An expired successor would be a fixture error, not this product witness.
        assert observed["b_key_survived_a_release"] is True, observed
        assert observed["b_key_unchanged_after_cas"] is True, observed
        assert (observed["b_result"], final.get("by")) == ("written", "B"), observed

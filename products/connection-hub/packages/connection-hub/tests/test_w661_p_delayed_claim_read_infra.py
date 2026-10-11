"""Independent actual-R/PG delayed claiming-read cut on the original P Source.

Only schedules the real _fetchrow call. SQL, results, guards and SDK locks are
unchanged. A's real Redis key expires; B takes a new key and reads/claims the
existing marker; then A's already-issued read/claim resumes. B must still write.
The no-stall control must pass. Not installed-runtime or full PB E2E proof.
"""
import asyncio
import json

import pytest

from connection_hub.delegated_credentials.cards import store as store_module
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_w661_p_row_write_owner_guard import TXN, world

W661_OWN_R_LOCK = True


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_claim", [False, True])
async def test_live_successor_can_commit_after_expired_read_claim_resumes(
        tmp_path, monkeypatch, stall_claim):
    async with world(tmp_path) as (store, rows, redis, key, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN,
                  "state": "staged", "members": []}
        async with held(0):
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)

        at_claim, resume_claim, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = {"stall_claim": stall_claim}
        actual_fetchrow = rows._fetchrow
        actual_guard = store_module._guard_row_write
        a_token = None

        async def record_actual_guard(path):
            await actual_guard(path)
            if asyncio.current_task().get_name() == "independent-claim-B":
                observed["b_real_owner_guard_passed"] = True

        async def schedule_actual_claim(query, *args):
            nonlocal a_token
            a_claim = (asyncio.current_task().get_name() == "independent-claim-A"
                       and query.lstrip().startswith("UPDATE")
                       and "connection_hub_card_version_txns" in query
                       and "SET fence" in query
                       and "RETURNING document, row_revision" in query)
            if a_claim:
                a_token = await redis.get(key)
                observed["a_key_present_before_claim_pause"] = bool(a_token)
                assert a_token
                at_claim.set()
                if stall_claim:
                    await asyncio.wait_for(resume_claim.wait(), 8)
                result = await actual_fetchrow(query, *args)
                observed["a_claim_result_revision"] = None if result is None else int(result["row_revision"])
                return result
            return await actual_fetchrow(query, *args)

        monkeypatch.setattr(rows, "_fetchrow", schedule_actual_claim)
        monkeypatch.setattr(store_module, "_guard_row_write", record_actual_guard)

        async def old_a():
            try:
                async with held(0):
                    try:
                        document = await store.read_marker_document(TXN)
                        observed["a_read_state"] = None if document is None else document.get("state")
                        assert document == staged
                        await store.write_marker_document(TXN, {**staged, "state": "published", "by": "A"})
                        observed["a_result"] = "written"
                    except CardStorageError as exc:
                        observed["a_result"] = str(exc)
            finally:
                a_done.set()

        async def successor_b():
            await asyncio.wait_for(at_claim.wait(), 5)
            if stall_claim:
                async def expires():
                    while await redis.exists(key):
                        await asyncio.sleep(.02)
                await asyncio.wait_for(expires(), 4)
            else:
                await asyncio.wait_for(a_done.wait(), 8)
            async with held(1):
                before = await store.read_marker_document(TXN)
                if stall_claim:
                    assert before == staged
                else:
                    assert before["by"] == "A"
                observed["b_initial_revision"] = (await rows.read_marker(TXN))[1]
                b_token = await redis.get(key)
                assert b_token and b_token != a_token
                observed["b_has_distinct_live_key_before_a_claim"] = True
                resume_claim.set()
                await asyncio.wait_for(a_done.wait(), 8)
                observed["b_key_survived_a_release"] = (await redis.get(key)) == b_token
                try:
                    await store.write_marker_document(TXN, {**staged, "state": "published", "by": "B"})
                    observed["b_result"] = "written"
                except CardStorageError as exc:
                    observed["b_result"] = str(exc)
                observed["b_key_unchanged_after_cas"] = (await redis.get(key)) == b_token

        a = asyncio.create_task(old_a(), name="independent-claim-A")
        b = asyncio.create_task(successor_b(), name="independent-claim-B")
        try:
            await asyncio.wait_for(asyncio.gather(a, b), 20)
        finally:
            resume_claim.set()
            await asyncio.gather(a, b, return_exceptions=True)

        final, revision = await rows.read_marker(TXN)
        observed["final_by"], observed["final_revision"] = final.get("by"), revision
        print("independent delayed claiming-read cut:", json.dumps(observed, sort_keys=True))
        assert observed["b_real_owner_guard_passed"] is True, observed
        assert observed["b_key_survived_a_release"] is True, observed
        assert observed["b_key_unchanged_after_cas"] is True, observed
        assert (observed["b_result"], final.get("by")) == ("written", "B"), observed

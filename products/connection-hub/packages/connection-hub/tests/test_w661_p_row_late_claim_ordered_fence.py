"""W700 P: a stale operation's LATE CLAIM never takes the row from the live successor (Ops' review of b1693251).

Ops' schedule: A passes its real owner guard and stalls just before its claiming read's UPDATE; A's key really
expires; B acquires the lock and its locked read claims the row; then A's delayed claim lands. With an UNORDERED
fence (b1693251's per-operation id) A's claim overwrites B's, nothing stale lands, but B's CAS is refused
card_version_marker_moved: the live owner loses its save. With the ORDERED fence token (one PostgreSQL sequence,
drawn before the owner guard) A's token predates B's, A's claim keeps B's fence (an unclaimed read), and B commits.

world() is Infra's actual-R fixture (SDK observed_redis_lock_async, 1 s TTL, renewal off to model the stall,
observed flock, real PG rows). The pause is scheduling only, at the real _fetchrow boundary of A's claiming
statement, after the production guard; no fake owner check, Redis DEL, clock change or SQL substitution. The
claim statement is matched by what it does (sets the fence, not the document), so the same witness runs on
b1693251 (where it fails) and on the ordered-token head (where it passes). The no-stall control must pass.
"""
import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
                                reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")

from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_w661_p_row_write_owner_guard import TXN, world

W661_OWN_R_LOCK = True


def _is_marker_claim(query: str) -> bool:
    return ("connection_hub_card_version_txns" in query and "SET fence" in query
            and "SET document" not in query)


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_before_claim", [False, True])
async def test_a_late_claim_of_an_expired_owner_never_refuses_the_successor(tmp_path, monkeypatch,
                                                                           stall_before_claim):
    async with world(tmp_path) as (store, rows, redis, key, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN, "state": "staged", "members": []}
        async with held(0):
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)

        at_claim, resume_claim, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = {"stall_before_claim": stall_before_claim}
        actual_fetchrow = rows._fetchrow

        async def schedule_actual_sql(query, *args):
            if asyncio.current_task().get_name() == "late-claim-A" and _is_marker_claim(query) \
                    and not at_claim.is_set():
                observed["a_key_present_at_claim"] = bool(await redis.get(key))
                at_claim.set()
                if stall_before_claim:
                    await asyncio.wait_for(resume_claim.wait(), 8)
            return await actual_fetchrow(query, *args)  # the unchanged statement and its unchanged result

        monkeypatch.setattr(rows, "_fetchrow", schedule_actual_sql)

        async def old_a():
            try:
                async with held(0):
                    try:
                        await store.read_marker_document(TXN)
                        await store.write_marker_document(TXN, {**staged, "state": "published", "by": "A"})
                        observed["a_result"] = "written"
                    except CardStorageError as exc:
                        observed["a_result"] = str(exc)
            except CardStorageError as exc:  # the lock's own release may report the loss too
                observed.setdefault("a_result", str(exc))
            finally:
                a_done.set()

        async def successor_b():
            await asyncio.wait_for(at_claim.wait(), 5)
            if stall_before_claim:
                async def expires():
                    while await redis.exists(key):
                        await asyncio.sleep(.02)
                await asyncio.wait_for(expires(), 4)  # observed real expiry, no DEL or clock substitution
            else:
                await asyncio.wait_for(a_done.wait(), 8)
            async with held(1):
                before = await store.read_marker_document(TXN)  # B's locked read claims the row
                observed["b_read_by"] = before.get("by")
                token = await redis.get(key)
                assert token
                resume_claim.set()  # A's delayed claim now lands (it keeps B's newer fence)
                await asyncio.wait_for(a_done.wait(), 8)
                try:
                    await store.write_marker_document(TXN, {**staged, "state": "published", "by": "B"})
                    observed["b_result"] = "written"
                except CardStorageError as exc:
                    observed["b_result"] = str(exc)
                observed["b_key_unchanged_after_cas"] = (await redis.get(key)) == token

        a = asyncio.create_task(old_a(), name="late-claim-A")
        b = asyncio.create_task(successor_b(), name="successor-B")
        try:
            await asyncio.wait_for(asyncio.gather(a, b), 20)
        finally:
            resume_claim.set()
            await asyncio.gather(a, b, return_exceptions=True)

        final, revision = await rows.read_marker(TXN)  # outside any operation: a plain read on either head
        observed["final_by"], observed["final_revision"] = final.get("by"), revision
        print("late-claim cut:", json.dumps(observed, sort_keys=True))
        assert observed["a_key_present_at_claim"] is True, observed  # A's claim came after a passed guard
        assert observed["b_key_unchanged_after_cas"] is True, observed  # B's own lease never lapsed
        assert (observed["b_result"], final.get("by")) == ("written", "B"), observed
        if stall_before_claim:
            assert observed["a_result"] != "written", observed  # the stale owner never writes
        else:
            assert (observed["a_result"], observed["b_read_by"]) == ("written", "A"), observed


# ---- the pointer row, at the row level (real PG; a NON-exclusive stand-in lock models a lock that was lost) ----

_S, _A, _X = "5" * 64, "a-1", "x-1"


def _pointer(n, by=""):
    return {"schema": "connection_hub.card-current.v1", "access_id": _A, "card_revision": n,
            "revision_name": f"r{n:08d}", "by": by}


@pytest.fixture
async def pointer_world(tmp_path):
    import asyncpg
    import contextlib
    import uuid

    from connection_hub.delegated_credentials.cards.locks import row_fenced_lock
    from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

    pool = await asyncpg.create_pool(os.environ["W661_TEST_PG_DSN"], min_size=1, max_size=4)
    project = "lc" + uuid.uuid4().hex[:12]
    rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
    await rows.ensure_schema()
    store = BundleStorageDelegatedCardStore(tmp_path)
    store.card_rows = rows

    @contextlib.asynccontextmanager
    async def lost_lock(*, lock_path, resource_id, operation, wait_seconds):
        yield {}  # no exclusion: two operations "hold" the same Card, as after an R lease expiry

    lock = row_fenced_lock(lost_lock)

    def card(access_id=_A):
        return lock(lock_path=store.root / f"{access_id}.lock", resource_id=f"delegated-card:{access_id}",
                    operation="test", wait_seconds=1.0)

    async with card():
        assert await store.read_pointer_document(subject_hash=_S, access_id=_A) is None
        await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(1))
    try:
        yield store, rows, card
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_pointer_a_claim_drawn_before_the_successor_never_takes_its_row(pointer_world):
    store, rows, card = pointer_world
    a_drawn, b_claimed, b_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = {}

    async def old_a():
        async with card():
            await rows.draw_fence()  # A drew its token (and passed its guard) before B acquired ...
            a_drawn.set()
            await b_claimed.wait()  # ... then stalled; B's locked read claimed the row meanwhile
            observed["a_read"] = await store.read_pointer_document(subject_hash=_S, access_id=_A)
            await b_done.wait()
            try:
                await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(9, "A"))
                observed["a"] = "written"
            except CardStorageError as exc:
                observed["a"] = str(exc)

    async def successor_b():
        await a_drawn.wait()
        async with card():
            assert (await store.read_pointer_document(subject_hash=_S, access_id=_A))["card_revision"] == 1
            b_claimed.set()
            await asyncio.sleep(0.2)  # A's late claim lands here
            try:
                await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(2, "B"))
                observed["b"] = "written"
            except CardStorageError as exc:
                observed["b"] = str(exc)
            b_done.set()

    await asyncio.wait_for(asyncio.gather(old_a(), successor_b()), 10)
    final, _revision = await rows.read_pointer(subject_hash=_S, access_id=_A)
    assert (observed["b"], observed["a"], final["by"]) == ("written", "card_pointer_moved", "B"), observed
    assert observed["a_read"]["card_revision"] == 1  # A's refused claim was an unclaimed read: fence, document, revision unchanged


@pytest.mark.asyncio
async def test_pointer_an_operation_without_the_cards_lock_reads_without_claiming(pointer_world):
    store, rows, card = pointer_world
    async with card():  # the Card's holder claims first, with the SMALLER token
        assert (await store.read_pointer_document(subject_hash=_S, access_id=_A))["card_revision"] == 1
        async with card(_X):  # nested: an unrelated Card acquired later, so a fresh, larger token ...
            pass
        other = asyncio.create_task(_read_under(card, store, _X))  # ... and another op holding only X reads A
        assert (await other)["card_revision"] == 1
        await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(2, "holder"))
    final, _revision = await rows.read_pointer(subject_hash=_S, access_id=_A)
    assert final["by"] == "holder"


async def _read_under(card, store, access_id):
    async with card(access_id):
        await store.card_rows.draw_fence()
        return await store.read_pointer_document(subject_hash=_S, access_id=_A)

"""Paging is additive; old bounded-read refusal and failure reporting stay intact."""
import pytest

from service_foundation.coordination.durable_decision_log import PagingDecisionStore
from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRefused, PostgresDecisionStore, RecoveryIncomplete,
)
from test_durable_decision_v2 import MemoryStore, Realm, Verifier, draft


class PagedStore(MemoryStore):
    async def list_in_doubt_page(self, *, limit, after=""):
        rows = sorted(await self.list_in_doubt(limit=limit), key=lambda r: r.transaction_id)
        rows = [row for row in rows if row.transaction_id > after]
        return rows[:limit], len(rows) > limit


@pytest.mark.asyncio
async def test_legacy_store_capability_and_refusal_are_unchanged():
    store = MemoryStore()
    manager = Coordinator(store, {}, Verifier())
    assert await manager.recover() == []
    with pytest.raises(DecisionRefused, match="recovery_paging_unavailable"):
        await manager.recover_page()
    for request in ("a", "b"):
        await store.begin(draft(request=request))
    with pytest.raises(DecisionRefused, match="recovery_unbounded"):
        await manager.recover(limit=1)
    assert store.decisions == []
    assert PagingDecisionStore.__name__ == "PagingDecisionStore"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1001, True, None, 1.5, "1"])
async def test_invalid_page_limits_refuse_before_storage(limit):
    manager = Coordinator(MemoryStore(), {}, Verifier())
    sql = PostgresDecisionStore(None, schema="test", namespace="test")
    for operation in (manager.recover_page, sql.list_in_doubt_page):
        with pytest.raises(DecisionRefused, match="recovery_limit_invalid"):
            await operation(limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [None, True, 1, [], {}, "x" * 129])
async def test_invalid_cursor_refuses_before_storage(after):
    manager = Coordinator(MemoryStore(), {}, Verifier())
    sql = PostgresDecisionStore(None, schema="test", namespace="test")
    for operation in (manager.recover_page, sql.list_in_doubt_page):
        with pytest.raises(DecisionRefused, match="recovery_cursor_invalid"):
            await operation(limit=1, after=after)


@pytest.mark.asyncio
async def test_empty_page_wraps_cursor_and_has_no_effect():
    assert await Coordinator(PagedStore(), {}, Verifier()).recover_page(after="last") == (
        [], "", False)


@pytest.mark.asyncio
async def test_failed_last_row_still_advances_scanned_cursor_and_wrap_retries():
    store = PagedStore()
    first = await store.begin(draft(request="a", expiry=1))
    failed = await store.begin(draft(request="b", expiry=1))
    realm = Realm(store, "card")
    original = realm.finish

    async def sometimes_unavailable(transaction_id, decision):
        if transaction_id == failed.transaction_id:
            raise RuntimeError("unavailable")
        return await original(transaction_id, decision)

    realm.finish = sometimes_unavailable
    manager = Coordinator(store, {"card": realm}, Verifier())
    with pytest.raises(RecoveryIncomplete) as failure:
        await manager.recover_page(limit=2)
    assert failure.value.next_after == failed.transaction_id
    assert failure.value.has_more is False
    assert set(failure.value.failures) == {failed.transaction_id}
    assert [row.transaction_id for row in failure.value.completed] == [first.transaction_id]
    assert (await store.read(failed.transaction_id)).finished == {}
    assert await manager.recover_page(after=failed.transaction_id) == ([], "", False)
    realm.finish = original
    rows, cursor, more = await manager.recover_page(after="")
    assert [row.transaction_id for row in rows] == [failed.transaction_id]
    assert cursor == failed.transaction_id and more is False


@pytest.mark.asyncio
async def test_legacy_failure_has_no_page_metadata():
    store = PagedStore()
    row = await store.begin(draft(request="a", expiry=1))
    with pytest.raises(RecoveryIncomplete) as failure:
        await Coordinator(store, {}, Verifier()).recover()
    assert set(failure.value.failures) == {row.transaction_id}
    assert failure.value.next_after is None and failure.value.has_more is None


@pytest.mark.asyncio
async def test_overfull_page_refuses_without_processing():
    store = PagedStore()
    for request in ("a", "b"):
        await store.begin(draft(request=request, expiry=1))

    async def unbounded(**kwargs):
        return list(store.rows.values()), True

    store.list_in_doubt_page = unbounded
    with pytest.raises(DecisionRefused, match="recovery_unbounded"):
        await Coordinator(store, {}, Verifier()).recover_page(limit=1)
    assert store.decisions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_more", [True, 1, None, "false"])
async def test_invalid_empty_page_metadata_refuses(has_more):
    store = PagedStore()

    async def invalid(**kwargs):
        return [], has_more

    store.list_in_doubt_page = invalid
    with pytest.raises(DecisionRefused, match="recovery_page_invalid"):
        await Coordinator(store, {}, Verifier()).recover_page()

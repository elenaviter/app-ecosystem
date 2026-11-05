"""Resident original lookup and bounded cleanup references in the existing SQL plan table."""

import pytest

from connection_hub.delegated_credentials.cards.resident_issuance import RESIDENT_ISSUANCE_KIND
from test_w603_issuance_store_postgres import _authority


async def _put(store, number, *, logical="a" * 64, attempt=0, kind=RESIDENT_ISSUANCE_KIND,
               bound=True, future=False):
    request, transaction = str(number) * 64, chr(ord("a") + number) * 64
    plan = {"issuance_kind": kind, "resident_request": logical, "resident_attempt": attempt,
            "decision_request_id": request, "synthetic_ref": "descriptor-reference"}
    now = await store.issuance_clock()
    await store.put_issuance_plan(decision_request_id=request, original_input_digest="e" * 64,
                                  plan=plan, reserved_until=now + 600 if future else now - 1)
    if bound:
        await store.bind_issuance_plan_transaction(decision_request_id=request, transaction_id=transaction)
    return request, transaction, plan


@pytest.mark.asyncio
async def test_latest_attempt_is_scoped_to_resident_purpose_and_logical_request():
    async with _authority() as (_, store):
        await _put(store, 1, attempt=0)
        _, transaction, plan = await _put(store, 2, attempt=1)
        await _put(store, 3, attempt=99, kind="external-oauth")
        await _put(store, 4, attempt=100, logical="b" * 64)
        assert await store.latest_resident_issuance("a" * 64) == {"plan": plan, "transaction_id": transaction}
        assert await store.latest_resident_issuance("f" * 64) is None


@pytest.mark.asyncio
async def test_cleanup_pages_are_bounded_references_excluding_future_unbound_and_other_purposes():
    async with _authority() as (_, store):
        first = await _put(store, 1)
        second = await _put(store, 2)
        await _put(store, 3, kind="external-oauth")
        await _put(store, 4, bound=False)
        await _put(store, 5, future=True)
        page = await store.resident_issuance_cleanup_page(limit=1)
        assert page == {"items": [{"decision_request_id": first[0], "transaction_id": first[1]}],
                        "next_after": first[0]}
        last = await store.resident_issuance_cleanup_page(after=page["next_after"], limit=1)
        assert last == {"items": [{"decision_request_id": second[0], "transaction_id": second[1]}],
                        "next_after": ""}
        assert await store.resident_issuance_cleanup_page(limit=1) == page  # wrap revisits immutable originals
        await store.complete_resident_original_cleanup(transaction_id=first[1])
        await store.complete_resident_original_cleanup(transaction_id=first[1])
        assert await store.resident_issuance_cleanup_page(limit=1) == last

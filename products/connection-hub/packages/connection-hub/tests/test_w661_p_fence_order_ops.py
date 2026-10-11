"""W700 P: the two orderings the ordered row fence rests on, each pinned by a witness that fails without it (Ops).

1. The token is drawn BEFORE the owner guard. A stale owner that passes its guard and then stalls must hold a
   token older than any successor's, so its later claim loses. Drawn after the guard, the stalled owner's token is
   newer than the successor's: it claims the row, reads the successor's revision and overwrites it.
2. The token is drawn AGAIN after every new lock acquisition. A token drawn under another Card before this Card's
   lock was acquired may predate a successor on this Card; reused, it loses the claim and the live owner's save
   is refused.

Each operation here runs in its OWN context (gather tasks start from the test's context, outside any lock), so
the successor is a separate Card operation with its own token. A task created inside an open lock section
inherits that section's operation and shares its token, and cannot witness either ordering.
"""
import asyncio
import contextvars
import os

import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
                                reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")

from connection_hub.delegated_credentials.cards import store as store_module
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_w661_p_row_late_claim_ordered_fence import _A, _S, _X, _pointer, pointer_world  # noqa: F401

W661_OWN_R_LOCK = True

_STALL_AFTER_GUARD = contextvars.ContextVar("stall_after_guard", default=None)


@pytest.mark.asyncio
async def test_pointer_an_owner_stalled_after_its_guard_never_overwrites_the_successor(pointer_world, monkeypatch):
    store, rows, card = pointer_world
    guarded, b_done = asyncio.Event(), asyncio.Event()
    real_guard = store_module._guard_row_write
    observed = {}

    async def guard_then_stall(path):
        await real_guard(path)  # the production owner guard passes ...
        if _STALL_AFTER_GUARD.get() and not guarded.is_set():
            guarded.set()
            await b_done.wait()  # ... then A stalls, its lock expires and B acquires, claims and commits

    monkeypatch.setattr(store_module, "_guard_row_write", guard_then_stall)

    async def stale_a():
        _STALL_AFTER_GUARD.set(True)
        async with card():
            observed["a_read"] = await store.read_pointer_document(subject_hash=_S, access_id=_A)
            try:
                await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(9, "A"))
                observed["a"] = "written"
            except CardStorageError as exc:
                observed["a"] = str(exc)

    async def successor_b():
        await guarded.wait()
        async with card():
            await store.read_pointer_document(subject_hash=_S, access_id=_A)
            await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(2, "B"))
        b_done.set()

    await asyncio.wait_for(asyncio.gather(stale_a(), successor_b()), 10)
    final, _revision = await rows.read_pointer(subject_hash=_S, access_id=_A)
    assert (observed["a"], final["by"]) == ("card_pointer_moved", "B"), (observed, final)


@pytest.mark.asyncio
async def test_pointer_a_later_acquisition_draws_after_a_separate_successor(pointer_world):
    store, rows, card = pointer_world
    drawn, b_done = asyncio.Event(), asyncio.Event()
    observed = {}

    async def operation_o():
        async with card(_X):
            await rows.draw_fence()  # token t1, drawn under X before A's lock was acquired
            drawn.set()
            await b_done.wait()  # a SEPARATE operation acquires A, claims with t_B > t1 and commits
            async with card():  # acquiring A must draw a token newer than t_B
                observed["read_by"] = (await store.read_pointer_document(subject_hash=_S, access_id=_A))["by"]
                try:
                    await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(3, "late"))
                    observed["o"] = "written"
                except CardStorageError as exc:
                    observed["o"] = str(exc)

    async def successor_b():
        await drawn.wait()
        async with card():
            await store.read_pointer_document(subject_hash=_S, access_id=_A)
            await store.write_pointer_document(subject_hash=_S, access_id=_A, document=_pointer(2, "other"))
        b_done.set()

    await asyncio.wait_for(asyncio.gather(operation_o(), successor_b()), 10)
    final, _revision = await rows.read_pointer(subject_hash=_S, access_id=_A)
    assert (observed["read_by"], observed["o"], final["by"]) == ("other", "written", "late"), (observed, final)

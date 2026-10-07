"""W578 S1 review: two disconnects of one account must not both hold its fence.

``reserve_accounts`` checks for a blocking fence, writes its own and checks
again. The write replaces ``fence.json`` unconditionally, so when the second
disconnect checks before the first writes and writes after the first's
recheck, both reserve, and the file names only the second. Its release (an
ABORT) then removes the only fence while the first disconnect is still in
flight, and a new binding of the account is admitted.

The interleaving is forced by wrapping the module's own check and write; no
other code is replaced.
"""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards.transaction_store import CardTransactionRefused
from test_w578_account_fence import ACCOUNT, DISCONNECT, OTHER, _world


async def _bounded(event):
    """Force the interleaving where the code allows it; under the account section it cannot occur (fix)."""
    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass


@pytest.mark.asyncio
async def test_a_second_disconnect_racing_the_first_cannot_unfence_it(tmp_path, monkeypatch):
    store, service, _card = await _world(tmp_path)
    real_blocking, real_write = fence._blocking_fence, fence.write_json_atomic
    other_checked, first_done = asyncio.Event(), asyncio.Event()
    checks = {}

    async def blocking(store_, provider, account, *, own):
        result = await real_blocking(store_, provider, account, own=own)
        checks[own] = checks.get(own, 0) + 1
        if own == OTHER and checks[own] == 1:
            other_checked.set()
            await _bounded(first_done)
        return result

    async def write(path, value):
        if value.get("transaction_id") == DISCONNECT and "accounts" in value:
            await _bounded(other_checked)
        return await real_write(path, value)

    monkeypatch.setattr(fence, "_blocking_fence", blocking)
    monkeypatch.setattr(fence, "write_json_atomic", write)

    async def first():
        try:
            await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
        finally:
            first_done.set()

    results = await asyncio.gather(first(), service.reserve_accounts([ACCOUNT], transaction_id=OTHER),
                                   return_exceptions=True)
    monkeypatch.setattr(fence, "_blocking_fence", real_blocking)
    monkeypatch.setattr(fence, "write_json_atomic", real_write)
    reserved = [r is None for r in results]
    assert reserved.count(True) == 1, f"exactly one disconnect may hold the account; got {results}"
    assert all(isinstance(r, CardTransactionRefused) for r in results if r is not None)

    loser = OTHER if reserved[0] else DISCONNECT
    await service.release_accounts(loser)
    with pytest.raises(fence.AccountFenceRefused, match="card_account_reserved"):
        await fence.mark_binding(store, [ACCOUNT], mark_id="direct-review", kind="direct",
                                 subject_hash="s", access_id="a")

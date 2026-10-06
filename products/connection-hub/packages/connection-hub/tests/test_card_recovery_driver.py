"""W502 (EMain 18:42): the Hub's recovery pass pages the in-doubt log with an exclusive cursor.

A fake ``recover_page`` with the kernel's agreed contract pins the driver:
bounded pages per pass, the cursor kept between passes and wrapped to "",
a failed row passed over (logged by id and code) and retried after the wrap,
and a kernel without the cursor contract never guessed past.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused, RecoveryIncomplete

from connection_hub.delegated_credentials.cards import composition


def _record(txid, done=True):
    return SimpleNamespace(transaction_id=txid, terminal=done, finished=("p",) if done else (),
                           intent=SimpleNamespace(participants=("p",)))


class _Paged:
    def __init__(self, ids, failing=(), cursor_contract=True):
        self.ids, self.failing, self.contract = sorted(ids), set(failing), cursor_contract
        self.calls, self.seen = [], []

    async def recover_page(self, *, limit, after):
        self.calls.append(after)
        rows = [txid for txid in self.ids if txid > after][: limit + 1]
        page, has_more = rows[:limit], len(rows) > limit
        next_after = page[-1] if page else ""
        self.seen.extend(page)
        failures = {txid: DecisionRefused("participant_unavailable") for txid in page if txid in self.failing}
        completed = [_record(txid) for txid in page if txid not in failures]
        if failures:
            exc = RecoveryIncomplete(failures, completed)
            if self.contract:
                exc.next_after, exc.has_more = next_after, has_more
            raise exc
        return completed, next_after, has_more


@pytest.mark.asyncio
async def test_a_backlog_larger_than_one_pass_drains_over_passes_each_row_once():
    ids = [f"{index:02d}" for index in range(7)]
    coordinator = _Paged(ids)
    first = await composition.recover_card_transactions(coordinator, limit=2, after="", max_pages=2)
    assert first == {"ok": True, "finished": 4, "pending": 0, "failed": 0, "pages": 2, "next_after": "03"}
    second = await composition.recover_card_transactions(coordinator, limit=2, after=first["next_after"],
                                                         max_pages=2)
    assert second == {"ok": True, "finished": 3, "pending": 0, "failed": 0, "pages": 2, "next_after": ""}
    assert coordinator.seen == ids  # every row exactly once across the two passes
    assert coordinator.calls == ["", "01", "03", "05"]


@pytest.mark.asyncio
async def test_a_failing_row_never_blocks_the_rows_after_it_and_is_retried_after_the_wrap(caplog):
    coordinator = _Paged(["a", "b", "c", "d"], failing={"b"})
    with caplog.at_level(logging.WARNING, logger="kdcube.connection_hub.card_transactions"):
        report = await composition.recover_card_transactions(coordinator, limit=2, max_pages=5)
    assert report == {"ok": False, "finished": 3, "pending": 0, "failed": 1, "pages": 2, "next_after": ""}
    assert coordinator.seen == ["a", "b", "c", "d"]
    assert any("transaction=b reason=participant_unavailable" in r.getMessage() for r in caplog.records)
    again = await composition.recover_card_transactions(coordinator, limit=2, after=report["next_after"])
    assert again["failed"] == 1 and coordinator.calls[-2:] == ["", "b"]  # retried after the wrap


@pytest.mark.asyncio
async def test_unexpired_rows_count_as_pending_and_are_paged_past():
    coordinator = _Paged(["a", "b", "c"])

    async def page(*, limit, after):
        records, next_after, has_more = await _Paged.recover_page(coordinator, limit=limit, after=after)
        return [_record(r.transaction_id, done=r.transaction_id != "a") for r in records], next_after, has_more

    coordinator.recover_page = page
    report = await composition.recover_card_transactions(coordinator, limit=2)
    assert (report["finished"], report["pending"], report["next_after"]) == (2, 1, "")


@pytest.mark.asyncio
async def test_a_kernel_without_the_cursor_contract_is_never_guessed_past():
    coordinator = _Paged(["a", "b"], failing={"a"}, cursor_contract=False)
    with pytest.raises(RecoveryIncomplete):
        await composition.recover_card_transactions(coordinator, limit=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [None, 7, "x" * 129])
async def test_an_unusable_stored_cursor_restarts_at_the_beginning(after):
    coordinator = _Paged(["a"])
    await composition.recover_card_transactions(coordinator, after=after)
    assert coordinator.calls == [""]

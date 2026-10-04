"""W419: a Card listing reads its per-Card data together, a few at a time.

Opening the project Card waited about a second in delegated_access_list, which
read each Card's control view and policies one after another. bounded_gather
keeps the input order, propagates the first failure as before, never runs more
than its bound at once, and takes about the longest read instead of their sum.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from connection_hub.concurrency import bounded_gather


def test_results_keep_input_order_and_the_bound_holds():
    running = 0
    peak = 0

    async def read(value: int) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01 * (5 - value % 5))
        running -= 1
        return value

    result = asyncio.run(bounded_gather([read(i) for i in range(20)], limit=4))
    assert result == list(range(20))
    assert peak == 4


def test_reads_overlap_instead_of_adding_up():
    async def read() -> None:
        await asyncio.sleep(0.05)

    started = time.monotonic()
    asyncio.run(bounded_gather([read() for _ in range(8)]))
    assert time.monotonic() - started < 0.2  # in turn it would be 0.4 s


def test_the_first_failure_propagates():
    async def ok() -> int:
        return 1

    async def broken() -> int:
        raise RuntimeError("card store unavailable")

    with pytest.raises(RuntimeError, match="card store unavailable"):
        asyncio.run(bounded_gather([ok(), broken(), ok()]))


def test_the_card_listing_and_its_policies_use_it():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    listing = (root / "packages/connection-hub/src/connection_hub/delegated_credentials/automation_access.py").read_text()
    entrypoint = (root / "apps/connection-hub@1-0/entrypoint.py").read_text()
    assert "control_views = await bounded_gather(" in listing
    assert "policies = await bounded_gather(" in entrypoint

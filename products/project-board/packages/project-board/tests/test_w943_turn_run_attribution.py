"""W943 F3: a slow turn names the store call it ran longest in, and its outbox send time.

After the lock fixes, dev-main's slow turns (12% over 5 s on 2026-10-23) no
longer waited on any lock: the executor's wait was 0.05 s while it ran a
median 3.5 s of store calls, and the turn summary named only the call behind
the longest wait.
"""

from __future__ import annotations

import asyncio

from project_board.client import relay, relay_trace
from project_board.client.relay_trace import EXECUTOR_RUN_LABELS, RelayActivityTrace


def test_the_summary_names_the_longest_run_and_the_labels_by_total_run_time() -> None:
    accounting = RelayActivityTrace().accounting
    turn = accounting.begin_turn("w1")
    turn.record_executor(0.01, 0.2, "store.SharedFieldStore.pull_mail")
    for _ in range(10):
        turn.record_executor(0.0, 0.3, "store.SharedFieldStore.sync_project_team")
    turn.record_executor(0.02, 1.5, "store.SharedFieldStore.reconcile_project_mailboxes_when_due")
    summary = accounting.end_turn(turn, "failed", code="fixture")

    executor = summary["executor"]
    assert executor["run_max_callable"] == "store.SharedFieldStore.reconcile_project_mailboxes_when_due"
    assert executor["run_seconds_max"] == 1.5
    # Many short calls can cost more than the single longest one.
    assert executor["run_top"][0] == ["store.SharedFieldStore.sync_project_team", 3.0, 10]
    assert [row[0] for row in executor["run_top"]] == [
        "store.SharedFieldStore.sync_project_team",
        "store.SharedFieldStore.reconcile_project_mailboxes_when_due",
        "store.SharedFieldStore.pull_mail",
    ]
    assert summary["outbox"] == {"sends": 0, "send_seconds_sum": 0.0}


def test_the_labels_kept_per_turn_are_bounded() -> None:
    accounting = RelayActivityTrace().accounting
    turn = accounting.begin_turn("w1")
    for index in range(EXECUTOR_RUN_LABELS + 20):
        turn.record_executor(0.0, 0.001, f"label.{index}")

    assert len(turn.executor_run_by_label) == EXECUTOR_RUN_LABELS
    assert len(accounting.end_turn(turn, "failed", code="fixture")["executor"]["run_top"]) == 3


def test_outbox_sends_and_uploads_are_timed_on_the_current_turn(monkeypatch) -> None:
    accounting = RelayActivityTrace().accounting

    class Board:
        async def action(self, **kwargs):
            await asyncio.sleep(0.05)
            return {"ok": True}

    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.client = Board()

    async def upload(files):
        await asyncio.sleep(0.02)
        return ["ref"]

    adapter._upload_attachments = upload

    async def turn_body():
        turn = accounting.begin_turn("w1")
        assert await adapter._outbox_action(object_ref="x", action="note", payload={}) == {"ok": True}
        assert await adapter._outbox_upload(["f"]) == ["ref"]
        return accounting.end_turn(turn, "failed", code="fixture")

    summary = asyncio.run(turn_body())

    assert summary["outbox"]["sends"] == 2
    assert summary["outbox"]["send_seconds_sum"] >= 0.07
    # Outside a turn the timer records nothing and raises nothing.
    relay_trace.record_outbox_send(1.0)


def test_a_failed_send_is_still_counted() -> None:
    accounting = RelayActivityTrace().accounting

    class Board:
        async def action(self, **kwargs):
            raise RuntimeError("transport")

    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.client = Board()

    async def turn_body():
        turn = accounting.begin_turn("w1")
        try:
            await adapter._outbox_action(object_ref="x", action="note", payload={})
        except RuntimeError:
            pass
        return accounting.end_turn(turn, "failed", code="transport")

    assert asyncio.run(turn_body())["outbox"]["sends"] == 1

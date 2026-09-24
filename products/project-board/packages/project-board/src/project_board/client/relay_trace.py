"""Bounded timing evidence for Problem Board relay cycles."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator


SLOW_RELAY_SECONDS = 5.0
TRACE_HISTORY_SECONDS = 300.0
TRACE_HISTORY_LIMIT = 1024
WAIT_CONTEXT_LIMIT = 32


class RelayActivityTrace:
    """Track active and recent relay work without retaining request payloads."""

    def __init__(
        self,
        *,
        slow_seconds: float = SLOW_RELAY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        log: logging.Logger | None = None,
    ) -> None:
        self.slow_seconds = max(0.001, float(slow_seconds))
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._log = log or logging.getLogger(__name__)
        self._next_cycle = 0
        self._next_stage = 0
        self._active_cycle = 0
        self._cycles: dict[int, tuple[float, float]] = {}
        self._active: dict[int, dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(
            maxlen=TRACE_HISTORY_LIMIT
        )

    def start_cycle(self) -> int:
        if self._active_cycle:
            raise RuntimeError("A relay cycle is already active.")
        self._next_cycle += 1
        cycle = self._next_cycle
        self._active_cycle = cycle
        self._cycles[cycle] = (self._monotonic(), self._wall_clock())
        return cycle

    def finish_cycle(self, cycle: int, *, outcome: str) -> dict[str, Any]:
        started = self._cycles.pop(cycle)
        ended_monotonic = self._monotonic()
        ended_at = self._wall_clock()
        total_seconds = max(0.0, ended_monotonic - started[0])
        stages = self._cycle_stages(cycle, ended_at=ended_at)
        summary = {
            "cycle": cycle,
            "outcome": str(outcome or "unknown"),
            "total_seconds": round(total_seconds, 3),
            "stages": stages,
        }
        if self._active_cycle == cycle:
            self._active_cycle = 0
        if total_seconds >= self.slow_seconds:
            self._log.warning(
                "Problem Board relay slow cycle cycle=%d outcome=%s "
                "total_seconds=%.3f threshold_seconds=%.3f stages=%s",
                cycle,
                summary["outcome"],
                total_seconds,
                self.slow_seconds,
                json.dumps(stages, separators=(",", ":"), sort_keys=True),
            )
        self._prune(ended_at)
        return summary

    @contextmanager
    def stage(
        self,
        stage: str,
        *,
        channel: str = "",
        operation: str = "",
    ) -> Iterator[None]:
        self._next_stage += 1
        token = self._next_stage
        record = {
            "cycle": self._active_cycle,
            "stage": str(stage or "unknown"),
            "channel": str(channel or ""),
            "operation": str(operation or ""),
            "started_monotonic": self._monotonic(),
            "started_at": self._wall_clock(),
        }
        self._active[token] = record
        outcome = "succeeded"
        try:
            yield
        except BaseException as exc:
            outcome = (
                "cancelled"
                if type(exc).__name__ == "CancelledError"
                else f"failed:{type(exc).__name__}"
            )
            raise
        finally:
            ended_monotonic = self._monotonic()
            ended_at = self._wall_clock()
            current = self._active.pop(token, record)
            self._history.append(
                {
                    **current,
                    "ended_at": ended_at,
                    "seconds": max(
                        0.0,
                        ended_monotonic
                        - float(current["started_monotonic"]),
                    ),
                    "outcome": outcome,
                }
            )
            self._prune(ended_at)

    def wait_context(
        self,
        queued_at: float,
        claimed_at: float,
    ) -> list[dict[str, Any]]:
        """Stages that overlapped a coordinate request's local queue wait."""

        start = min(float(queued_at), float(claimed_at))
        end = max(float(queued_at), float(claimed_at))
        candidates = list(self._history)
        candidates.extend(
            {
                **record,
                "ended_at": end,
                "seconds": max(0.0, end - float(record["started_at"])),
                "outcome": "in_progress",
            }
            for record in self._active.values()
        )
        overlaps: list[tuple[float, dict[str, Any]]] = []
        for record in candidates:
            record_start = float(record["started_at"])
            record_end = float(record.get("ended_at") or end)
            overlap = min(end, record_end) - max(start, record_start)
            if overlap <= 0:
                continue
            overlaps.append(
                (
                    overlap,
                    {
                        "cycle": int(record.get("cycle") or 0),
                        "stage": str(record.get("stage") or "unknown"),
                        "channel": str(record.get("channel") or ""),
                        "operation": str(record.get("operation") or ""),
                        "overlap_seconds": round(overlap, 3),
                    },
                )
            )
        if len(overlaps) > WAIT_CONTEXT_LIMIT:
            overlaps = sorted(overlaps, key=lambda item: item[0], reverse=True)[
                :WAIT_CONTEXT_LIMIT
            ]
        return [
            item
            for _, item in sorted(
                overlaps,
                key=lambda value: (
                    value[1]["cycle"],
                    value[1]["stage"],
                    value[1]["channel"],
                ),
            )
        ]

    def _cycle_stages(
        self,
        cycle: int,
        *,
        ended_at: float,
    ) -> list[dict[str, Any]]:
        records = [
            record for record in self._history if record.get("cycle") == cycle
        ]
        records.extend(
            {
                **record,
                "ended_at": ended_at,
                "seconds": max(
                    0.0,
                    ended_at - float(record["started_at"]),
                ),
                "outcome": "in_progress",
            }
            for record in self._active.values()
            if record.get("cycle") == cycle
        )
        return [
            {
                "stage": str(record.get("stage") or "unknown"),
                "channel": str(record.get("channel") or ""),
                "operation": str(record.get("operation") or ""),
                "seconds": round(float(record.get("seconds") or 0.0), 3),
                "outcome": str(record.get("outcome") or "unknown"),
            }
            for record in sorted(
                records,
                key=lambda item: (
                    float(item.get("started_at") or 0.0),
                    str(item.get("stage") or ""),
                ),
            )
        ]

    def _prune(self, now: float) -> None:
        cutoff = float(now) - TRACE_HISTORY_SECONDS
        while self._history and float(self._history[0]["ended_at"]) < cutoff:
            self._history.popleft()


__all__ = ["RelayActivityTrace", "SLOW_RELAY_SECONDS"]

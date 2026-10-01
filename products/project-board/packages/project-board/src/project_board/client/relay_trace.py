"""Bounded timing evidence for Problem Board relay cycles."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
import logging
import os
import sys
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator


SLOW_RELAY_SECONDS = 5.0
TRACE_HISTORY_SECONDS = 300.0
TRACE_HISTORY_LIMIT = 1024
WAIT_CONTEXT_LIMIT = 32
# W448: the relay serves every channel on one event loop. A sampler sleeps
# this long and measures how late it wakes; a late wake means the loop or the
# whole process did not run (a blocking call, a paging host), which no
# transport timeout can tell apart from a slow server.
LOOP_LAG_INTERVAL_SECONDS = 1.0
LOOP_STALL_SECONDS = 1.0
LOOP_STALL_LOG_EVERY_SECONDS = 30.0


# libproc's PROC_PIDTASKINFO and the size of struct proc_taskinfo: six
# uint64 (virtual, resident, four times) then twelve int32 counters.
_PROC_PIDTASKINFO = 4
_PROC_TASKINFO_SIZE = 96
_libproc: Any = None


def _darwin_resident_bytes() -> int | None:
    """Current resident size from libproc, in process, without a subprocess."""

    global _libproc
    if _libproc is None:
        path = ctypes.util.find_library("proc")
        if not path:
            _libproc = False
            return None
        _libproc = ctypes.CDLL(path, use_errno=True)
    if _libproc is False:
        return None
    buffer = ctypes.create_string_buffer(_PROC_TASKINFO_SIZE)
    written = _libproc.proc_pidinfo(
        os.getpid(), _PROC_PIDTASKINFO, ctypes.c_uint64(0), buffer, _PROC_TASKINFO_SIZE
    )
    if written != _PROC_TASKINFO_SIZE:
        return None
    return int.from_bytes(buffer.raw[8:16], sys.byteorder)


def process_memory() -> dict[str, int]:
    """This process's resident memory, where the platform reports it.

    ``rss_bytes`` is the current resident set (Linux ``/proc``, macOS
    libproc); ``rss_peak_bytes`` is the lifetime peak from ``getrusage``
    (bytes on macOS, kilobytes on Linux). A missing source leaves its key out.
    """

    memory: dict[str, int] = {}
    try:
        if sys.platform == "darwin":
            resident = _darwin_resident_bytes()
            if resident:
                memory["rss_bytes"] = resident
        else:
            with open("/proc/self/statm", encoding="ascii") as statm:
                pages = int(statm.read().split()[1])
            memory["rss_bytes"] = pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        memory["rss_peak_bytes"] = peak if sys.platform == "darwin" else peak * 1024
    except (ImportError, OSError, ValueError):
        pass
    return memory


def major_faults() -> int | None:
    """This process's major page faults so far (Linux and macOS getrusage)."""

    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_majflt)
    except (ImportError, OSError, ValueError):
        return None


def memory_log_fields() -> str:
    """The memory fields for a log line, naming which source they came from.

    Without ``/proc`` (macOS) only the lifetime peak is known, and a peak
    never falls, so the line says ``rss_source=peak_only`` rather than let it
    pass for current memory.
    """

    memory = process_memory()
    source = "current" if "rss_bytes" in memory else (
        "peak_only" if memory else "unavailable"
    )
    return "".join(
        f" {key}={value}" for key, value in sorted(memory.items())
    ) + f" rss_source={source}"


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
        # Largest loop lag since the last finished cycle, and the stalls not
        # yet logged.
        self._loop_lag_max = 0.0
        self._stall_count = 0
        self._stall_max = 0.0
        self._stall_logged_at: float | None = None
        # Major page faults at the previous stall or slow-cycle line: the
        # delta says whether the relay itself was paging (W448).
        self._major_faults_at_line = major_faults()

    def _paging_log_fields(self) -> str:
        current = major_faults()
        previous = self._major_faults_at_line
        self._major_faults_at_line = current
        fields = memory_log_fields()
        if current is not None and previous is not None:
            fields = f" major_faults_delta={max(0, current - previous)}" + fields
        return fields

    def record_loop_lag(self, lag_seconds: float) -> None:
        """Keep one sampler measurement; log stalls at most every 30 s."""

        lag = max(0.0, float(lag_seconds))
        self._loop_lag_max = max(self._loop_lag_max, lag)
        if lag < LOOP_STALL_SECONDS:
            return
        self._stall_count += 1
        self._stall_max = max(self._stall_max, lag)
        now = self._monotonic()
        if (
            self._stall_logged_at is not None
            and now - self._stall_logged_at < LOOP_STALL_LOG_EVERY_SECONDS
        ):
            return
        self._log.warning(
            "Problem Board relay loop stalled max_lag_seconds=%.3f stalls=%d "
            "threshold_seconds=%.3f%s",
            self._stall_max,
            self._stall_count,
            LOOP_STALL_SECONDS,
            self._paging_log_fields(),
        )
        self._stall_logged_at = now
        self._stall_count = 0
        self._stall_max = 0.0

    async def sample_loop_lag(
        self, interval_seconds: float = LOOP_LAG_INTERVAL_SECONDS
    ) -> None:
        """Measure, until cancelled, how late the event loop wakes a sleeper."""

        interval = max(0.01, float(interval_seconds))
        while True:
            started = time.monotonic()
            await asyncio.sleep(interval)
            self.record_loop_lag(time.monotonic() - started - interval)

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
            "loop_lag_max_seconds": round(self._loop_lag_max, 3),
            "stages": stages,
        }
        self._loop_lag_max = 0.0
        if self._active_cycle == cycle:
            self._active_cycle = 0
        if total_seconds >= self.slow_seconds:
            self._log.warning(
                "Problem Board relay slow cycle cycle=%d outcome=%s "
                "total_seconds=%.3f threshold_seconds=%.3f "
                "loop_lag_max_seconds=%.3f%s stages=%s",
                cycle,
                summary["outcome"],
                total_seconds,
                self.slow_seconds,
                summary["loop_lag_max_seconds"],
                self._paging_log_fields(),
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


__all__ = [
    "RelayActivityTrace",
    "SLOW_RELAY_SECONDS",
    "major_faults",
    "memory_log_fields",
    "process_memory",
]

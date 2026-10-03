"""Bounded timing evidence for Problem Board relay cycles."""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import ctypes.util
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterator

from .off_loop import OFF_LOOP_OBSERVER


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
# W456: a stalled loop reports its stall only after it runs again, and then
# only the length. A watchdog thread reads the loop thread's Python stack
# while the stall is happening, so the line names the code holding the loop.
# The sampler beats every LOOP_LAG_INTERVAL_SECONDS, so a beat older than this
# means the loop has not run for at least two seconds.
LOOP_BLOCKED_SECONDS = 3.0
LOOP_WATCHDOG_INTERVAL_SECONDS = 0.5
LOOP_BLOCKED_FRAMES = 12


# W461: turn and job accounting. A channel turn gets an opaque id; its stages,
# coordinate requests and executor calls carry that id through the task's
# context, never through timestamps. Only slow, failed or cancelled turns
# write a summary line; every turn is counted in 900 s buckets. The ceilings
# below are upper bounds, not output targets. Lines go to the relay's existing
# rotating log (relay_logging: a nominal 40 MiB ring shared with every other
# relay line, so how far back it reaches depends on all of that traffic).
ACCOUNTING_BUCKET_SECONDS = 900.0
ACCOUNTING_RETAINED_BUCKETS = 48
ACCOUNTING_RETAINED_BYTES = 256 * 1024
ACCOUNTING_MAX_WORKERS = 16
ACCOUNTING_MAX_OVERFLOW_IDS = 64
ACCOUNTING_STARTUP_SECONDS = 120.0
SUMMARY_MAX_BYTES = 8 * 1024
SUMMARY_MAX_STAGES = 24
SUMMARY_MAX_REQUESTS = 16
SUMMARY_BYTES_PER_MINUTE = 1024 * 1024
# The relay formatter puts a UTC timestamp, the level and the logger name in
# front of each line; the byte bounds count it.
LOG_PREFIX_RESERVE_BYTES = 128
DURATION_EDGES_SECONDS = (1.0, 5.0, 10.0, 20.0, 40.0)
JOB_KINDS = frozenset({"workspace_size"})
JOB_EVENTS = frozenset(
    {
        "schedule_attempted",
        "coalesced_in_flight",
        "not_due",
        "accepted",
        "walk_started",
        "completed",
        "failed",
        "cancelled",
    }
)
TURN_OUTCOMES = ("succeeded", "failed", "cancelled", "deadline", "abandoned_at_shutdown")
TURN_SUMMARY_MESSAGE = "Problem Board relay turn summary v1 "
ACCOUNTING_MESSAGE = "Problem Board relay turn accounting v1 "

# Context keys, not caches: each holds the value of the task that set it.
_TURN: contextvars.ContextVar["_TurnState | None"] = contextvars.ContextVar(
    "problem_board_relay_turn", default=None
)
_REQUEST: contextvars.ContextVar[str] = contextvars.ContextVar(
    "problem_board_relay_request", default=""
)


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


class LoopStallWatchdog:
    """Name the Python frames holding the relay's event loop during a stall.

    The loop records a beat; a daemon thread checks it. When the beat is older
    than ``blocked_seconds`` the thread reads the loop thread's current stack
    and logs its innermost frames (file name, line, function; never locals),
    once per stall and at most every ``log_every_seconds``.
    """

    def __init__(
        self,
        *,
        log: logging.Logger,
        blocked_seconds: float = LOOP_BLOCKED_SECONDS,
        interval_seconds: float = LOOP_WATCHDOG_INTERVAL_SECONDS,
        log_every_seconds: float = LOOP_STALL_LOG_EVERY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self.blocked_seconds = max(0.001, float(blocked_seconds))
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.log_every_seconds = max(0.0, float(log_every_seconds))
        self._monotonic = monotonic
        self._beat = monotonic()
        self._loop_thread_id: int | None = None
        self._reported_beat: float | None = None
        self._logged_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        self._beat = self._monotonic()

    def start(self) -> None:
        """Watch the calling thread, which runs the loop."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._loop_thread_id = threading.get_ident()
        self.beat()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="problem-board-relay-loop-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - diagnostics never stop the relay
                self._log.debug("Problem Board relay loop watchdog check failed", exc_info=True)

    def check_once(self) -> dict[str, Any] | None:
        """Log and return the loop thread's frames when the loop is blocked now."""

        beat = self._beat
        now = self._monotonic()
        blocked = now - beat
        if blocked < self.blocked_seconds or self._reported_beat == beat:
            return None
        if (
            self._logged_at is not None
            and now - self._logged_at < self.log_every_seconds
        ):
            return None
        thread_id = self._loop_thread_id
        frame = sys._current_frames().get(thread_id) if thread_id is not None else None
        if frame is None:
            return None
        frames = [
            f"{Path(entry.filename).name}:{entry.lineno}:{entry.name}"
            for entry in traceback.extract_stack(frame)[-LOOP_BLOCKED_FRAMES:]
        ]
        self._reported_beat = beat
        self._logged_at = now
        self._log.warning(
            "Problem Board relay loop blocked blocked_seconds=%.3f "
            "threshold_seconds=%.3f frames=%s",
            blocked,
            self.blocked_seconds,
            json.dumps(frames, separators=(",", ":")),
        )
        return {"blocked_seconds": round(blocked, 3), "frames": frames}

# Only these names leave the process in a summary; anything else is "other"
# (W461 review: reject unknown labels, never sanitize by blacklist).
STAGE_NAMES = frozenset(
    {
        "attendance.controls",
        "attendance.heartbeat",
        "attendance.outbox",
        "attendance.poll",
        "channel.close",
        "channel.open",
        "channel.reconnect",
        "channel.registration",
        "channels.retire_local",
        "channels.turns",
        "coordinate.drain",
        "host.load",
        "native_queue.reconcile",
        "project.poll",
        "session.notify",
        "session.notify_lock_wait",
        "startup_recovery",
    }
)
OPERATION_NAMES = frozenset(
    {
        "attendance.reconcile",
        "card.resolve_credential",
        "channel.turn_grace",
        "control.pull",
        "control.pull.discovery",
        "control_plane.connected",
        "coordinate.claim_and_execute",
        "data_bus.connect",
        "data_bus.wait_until_connected",
        "inactive_session.close",
        "input.available",
        "mailbox.reconciliation",
        "outbox.flush",
        "project.reconcile",
        "relay.config",
        "session_queue.preflight",
        "terminal_listener.reconcile",
        "worker.heartbeat",
        "worker.heartbeat.discovery",
        "worker.publish",
    }
)


ERROR_CLASSES = frozenset(
    {
        "CancelledError",
        "ConnectionError",
        "DomainError",
        "OSError",
        "RelayStageError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
    }
)


def allowlisted(value: str, names: frozenset[str]) -> str:
    value = str(value or "")
    if not value:
        return ""
    return value if value in names else "other"


def duration_bucket(seconds: float) -> int:
    """Index of ``seconds`` in the fixed histogram: <1, 1-5, 5-10, 10-20, 20-40, >=40."""

    for index, edge in enumerate(DURATION_EDGES_SECONDS):
        if seconds < edge:
            return index
    return len(DURATION_EDGES_SECONDS)


def _new_histogram() -> list[int]:
    return [0] * (len(DURATION_EDGES_SECONDS) + 1)


def _utc(epoch: float) -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
        + f".{int((epoch % 1) * 1000):03d}Z"
    )


def _encoded_size(message: str, payload: dict[str, Any]) -> int:
    line = message + json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return len(line.encode("utf-8")) + LOG_PREFIX_RESERVE_BYTES


@dataclass
class _TurnState:
    """One channel turn while it runs. Held by its task's context, never shared."""

    turn_id: str
    worker: str
    startup: bool
    started_monotonic: float
    started_at: float
    finished: bool = False
    executor_calls: int = 0
    executor_wait_seconds: float = 0.0
    executor_wait_max_seconds: float = 0.0
    executor_run_seconds: float = 0.0
    requests: list[str] = field(default_factory=list)
    omitted_requests: int = 0
    tokens: tuple[Any, ...] = field(default=(), repr=False)

    def record_executor(self, wait_seconds: float, run_seconds: float) -> None:
        if self.finished:
            return
        self.executor_calls += 1
        self.executor_wait_seconds += wait_seconds
        self.executor_wait_max_seconds = max(self.executor_wait_max_seconds, wait_seconds)
        self.executor_run_seconds += run_seconds

    def add_request(self, request_id: str) -> None:
        if not request_id or self.finished:
            return
        if len(self.requests) < SUMMARY_MAX_REQUESTS:
            self.requests.append(request_id)
        else:
            self.omitted_requests += 1


class RequestScope:
    """The coordinate request a relay drain is executing, set per request and always reset."""

    def __init__(self) -> None:
        self._token: Any = None

    def enter(self, request_id: str) -> None:
        self.close()
        self._token = _REQUEST.set(str(request_id or ""))
        turn = _TURN.get()
        if turn is not None:
            turn.add_request(str(request_id or ""))

    def close(self) -> None:
        if self._token is not None:
            _REQUEST.reset(self._token)
            self._token = None


def _new_counts() -> dict[str, Any]:
    return {
        "started": 0,
        **{outcome: 0 for outcome in TURN_OUTCOMES},
        "seconds_sum": 0.0,
        "seconds_max": 0.0,
        "histogram": _new_histogram(),
    }


class _Bucket:
    def __init__(self, *, seq: int, started_monotonic: float, started_at: float) -> None:
        self.seq = seq
        self.started_monotonic = started_monotonic
        self.started_at = started_at
        self.hosts = {"startup": _new_counts(), "steady": _new_counts()}
        self.workers: dict[str, dict[str, Any]] = {}
        self.overflow_ids: set[str] = set()
        self.overflow_ids_capped = False
        self.executor = {
            "calls": 0,
            "wait_seconds_sum": 0.0,
            "wait_seconds_max": 0.0,
            "run_seconds_sum": 0.0,
            "wait_histogram": _new_histogram(),
        }
        self.jobs: dict[str, dict[str, Any]] = {}
        self.summaries_queued = 0
        self.summaries_dropped_rate = 0
        self.summaries_dropped_pending = 0

    def worker_counts(self, worker: str) -> dict[str, Any]:
        if worker in self.workers:
            return self.workers[worker]
        if len(self.workers) < ACCOUNTING_MAX_WORKERS:
            self.workers[worker] = _new_counts()
            return self.workers[worker]
        if worker not in self.overflow_ids:
            if len(self.overflow_ids) < ACCOUNTING_MAX_OVERFLOW_IDS:
                self.overflow_ids.add(worker)
            else:
                self.overflow_ids_capped = True
        return self.workers.setdefault("other", _new_counts())

    def job(self, kind: str) -> dict[str, Any]:
        return self.jobs.setdefault(
            kind,
            {
                **{event: 0 for event in sorted(JOB_EVENTS)},
                "queue_wait_histogram": _new_histogram(),
                "run_histogram": _new_histogram(),
            },
        )

    def payload(self, *, process_id: str, ended_monotonic: float, ended_at: float, final: bool) -> dict[str, Any]:
        def rounded(counts: dict[str, Any]) -> dict[str, Any]:
            return {
                **counts,
                "seconds_sum": round(counts["seconds_sum"], 3),
                "seconds_max": round(counts["seconds_max"], 3),
            }

        return {
            "event": "bucket",
            "schema": 1,
            "process_id": process_id,
            "seq": self.seq,
            "final": final,
            "started_at": _utc(self.started_at),
            "ended_at": _utc(ended_at),
            "covered_seconds": round(max(0.0, ended_monotonic - self.started_monotonic), 3),
            "duration_edges_seconds": list(DURATION_EDGES_SECONDS),
            "host_turns": {phase: rounded(c) for phase, c in self.hosts.items()},
            "worker_turns": {name: rounded(c) for name, c in sorted(self.workers.items())},
            "worker_turns_other_is_rollup": "other" in self.workers,
            "overflow_worker_ids": len(self.overflow_ids),
            "overflow_worker_ids_capped": self.overflow_ids_capped,
            "executor": {
                **self.executor,
                "wait_seconds_sum": round(self.executor["wait_seconds_sum"], 3),
                "wait_seconds_max": round(self.executor["wait_seconds_max"], 3),
                "run_seconds_sum": round(self.executor["run_seconds_sum"], 3),
            },
            "jobs": dict(sorted(self.jobs.items())),
            "summaries": {
                "queued": self.summaries_queued,
                "dropped_rate_cap": self.summaries_dropped_rate,
                "dropped_pending_budget": self.summaries_dropped_pending,
            },
        }


def _consume_outcome(waiter: "asyncio.Future[Any]") -> None:
    if not waiter.cancelled():
        waiter.exception()


class DiagnosticWriter:
    """Writes diagnostic lines in one daemon thread of its own, one at a time.

    W461 review: a file write has no wall-clock bound, so it never runs on the
    relay's event loop, in a channel's store thread or in the default pool.
    The thread is a daemon owned by this writer: a write stuck in the file
    system never holds relay stop or interpreter exit (a ThreadPoolExecutor
    thread would be joined at exit). Its queue holds one write at most, so at
    most one write is in flight and nothing piles up behind a stuck one. A
    write not finished within ``timeout_seconds`` is *unknown*; it is
    reconciled once when it finishes, and no other write starts before then.

    ``sink(line)`` must raise when the line was not appended; its return is
    the only success receipt (appended and flushed under the sink's contract,
    not a durability guarantee). A failure is recorded by error class only,
    never by its text.
    """

    def __init__(
        self,
        sink: Callable[[str], None] | None = None,
        *,
        submit: Callable[[str], Any] | None = None,
        timeout_seconds: float = 0.5,
    ) -> None:
        import queue

        if (sink is None) == (submit is None):
            raise ValueError("give exactly one of sink or submit")
        # ``submit(line)`` hands the line to an owner thread that already
        # exists (the relay log owner) and returns its receipt future, or None
        # when the owner's bounded queue refused it. ``sink(line)`` is written
        # by this writer's own daemon thread instead.
        self._sink = sink
        self._submit = submit
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self._jobs: "queue.Queue[tuple[Any, str] | None]" = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._outstanding: Any = None
        self._outstanding_key: tuple[str, int] | None = None
        self._closed = False
        self.receipts = {
            "appended": 0,
            "failed": 0,
            "unknown_at_timeout": 0,
            "late_appended": 0,
            "late_failed": 0,
            "dropped_by_owner": 0,
        }
        self.failure_classes: dict[str, int] = {}
        self.last_appended: tuple[str, int] | None = None
        # Told once about each write whose outcome arrived after its caller
        # stopped waiting: (key, appended). The accounting counts a bucket
        # that definitely failed as lost there, exactly once.
        self.on_late: Callable[[tuple[str, int], bool], None] | None = None

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, line = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                self._sink(line)
            except BaseException as exc:  # noqa: BLE001 - recorded by class only
                future.set_exception(exc)
            else:
                future.set_result(None)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="problem-board-diagnostics", daemon=True
            )
            self._thread.start()

    def _failed(self, future: Any) -> bool:
        if future.cancelled():
            self.failure_classes["cancelled"] = self.failure_classes.get("cancelled", 0) + 1
            return True
        error = future.exception()
        if error is None:
            return False
        name = allowlisted(type(error).__name__, ERROR_CLASSES) or "other"
        self.failure_classes[name] = self.failure_classes.get(name, 0) + 1
        return True

    def busy(self) -> bool:
        self._reconcile()
        return self._outstanding is not None

    def _reconcile(self) -> None:
        future = self._outstanding
        if future is None or not future.done():
            return
        key = self._outstanding_key
        self._outstanding = None
        self._outstanding_key = None
        appended = not self._failed(future)
        if appended:
            self.receipts["late_appended"] += 1
            self.last_appended = key
        else:
            self.receipts["late_failed"] += 1
        if self.on_late is not None and key is not None:
            try:
                self.on_late(key, appended)
            except Exception:  # noqa: BLE001 - accounting never fails a write
                pass

    async def write(self, key: tuple[str, int], line: str) -> str:
        """``appended``, ``failed``, ``unknown`` (still running), ``dropped`` or ``busy``.

        The single in-flight slot is taken before the first suspension, so a
        second caller sees ``busy``, and it stays taken when this caller is
        cancelled while waiting: that write is reconciled once when it ends.
        """

        import concurrent.futures

        if self._closed or self.busy():
            return "busy"
        if self._submit is not None:
            submitted = self._submit(line)
            if submitted is None:
                self.receipts["dropped_by_owner"] += 1
                return "dropped"
            future = submitted
        else:
            self._ensure_thread()
            future = concurrent.futures.Future()
            self._jobs.put_nowait((future, line))
        self._outstanding = future
        self._outstanding_key = key
        waiter = asyncio.wrap_future(future)
        # Retrieve the wrapper's outcome whenever it comes, so its exception
        # (whose text may name a path) never reaches the loop's handler.
        waiter.add_done_callback(_consume_outcome)
        done, _ = await asyncio.wait({waiter}, timeout=self.timeout_seconds)
        if waiter not in done:
            self.receipts["unknown_at_timeout"] += 1
            return "unknown"
        self._outstanding = None
        self._outstanding_key = None
        if self._failed(future):
            self.receipts["failed"] += 1
            return "failed"
        self.receipts["appended"] += 1
        self.last_appended = key
        return "appended"

    def close(self) -> None:
        """Stop taking writes; never waits for one in flight (its outcome stays unknown)."""

        import queue

        self._closed = True
        if self._thread is not None:
            try:
                self._jobs.put_nowait(None)
            except queue.Full:
                pass


class TurnAccounting:
    """Counts every channel turn and job; queues bounded lines for the writer.

    Lives on the relay's ``RelayActivityTrace`` (one per relay process). After
    an abrupt stop nothing of the unwritten tail survives: the next process
    says so (``prior_coverage: uncertain``) and never invents counts for it.
    """

    def __init__(
        self,
        *,
        writer: DiagnosticWriter | None,
        slow_seconds: float,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
        process_id: str | None = None,
    ) -> None:
        self._writer = writer
        self.slow_seconds = slow_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self.process_id = process_id or os.urandom(8).hex()
        self._process_started = monotonic()
        self._next_turn = 0
        self._next_seq = 0
        self._active: dict[str, _TurnState] = {}
        self._bucket = self._open_bucket()
        # (kind, seq, line, size), oldest first; buckets and summaries share it.
        self._pending: deque[tuple[str, int, str, int]] = deque()
        self._pending_bytes = 0
        self._pending_buckets = 0
        self.buckets_lost_unwritten = 0
        self._rate_window_started = monotonic()
        self._rate_window_bytes = 0
        self._announced = False
        self._shut_down = False
        if writer is not None:
            writer.on_late = self._late_receipt

    def _late_receipt(self, key: tuple[str, int], appended: bool) -> None:
        if not appended and key[0] == "bucket":
            self.buckets_lost_unwritten += 1

    def _open_bucket(self) -> _Bucket:
        self._next_seq += 1
        return _Bucket(
            seq=self._next_seq,
            started_monotonic=self._monotonic(),
            started_at=self._wall_clock(),
        )

    # Turns -------------------------------------------------------------
    def begin_turn(self, worker: str) -> _TurnState:
        self._next_turn += 1
        now = self._monotonic()
        turn = _TurnState(
            turn_id=f"{self.process_id}:{self._next_turn}",
            worker=str(worker or "-"),
            startup=now - self._process_started < ACCOUNTING_STARTUP_SECONDS,
            started_monotonic=now,
            started_at=self._wall_clock(),
        )
        turn.tokens = (_TURN.set(turn), OFF_LOOP_OBSERVER.set(turn.record_executor))
        self._active[turn.turn_id] = turn
        phase = "startup" if turn.startup else "steady"
        self._bucket.hosts[phase]["started"] += 1
        self._bucket.worker_counts(turn.worker)["started"] += 1
        return turn

    def end_turn(
        self,
        turn: _TurnState,
        outcome: str,
        *,
        code: str = "",
        stages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Count the turn once; return its summary when one is queued."""

        if turn.finished:
            return None
        turn.finished = True
        turn_token, observer_token = turn.tokens
        try:
            OFF_LOOP_OBSERVER.reset(observer_token)
            _TURN.reset(turn_token)
        except ValueError:
            # Reset from another context: the values die with that context.
            pass
        self._active.pop(turn.turn_id, None)
        outcome = outcome if outcome in TURN_OUTCOMES else "failed"
        seconds = max(0.0, self._monotonic() - turn.started_monotonic)
        self._count(turn, outcome, seconds)
        bucket = self._bucket
        bucket.executor["calls"] += turn.executor_calls
        bucket.executor["wait_seconds_sum"] += turn.executor_wait_seconds
        bucket.executor["run_seconds_sum"] += turn.executor_run_seconds
        bucket.executor["wait_seconds_max"] = max(
            bucket.executor["wait_seconds_max"], turn.executor_wait_max_seconds
        )
        if turn.executor_calls:
            bucket.executor["wait_histogram"][duration_bucket(turn.executor_wait_max_seconds)] += 1
        if outcome == "succeeded" and seconds < self.slow_seconds:
            return None
        summary = self._summary(turn, outcome, code, seconds, stages or [])
        self._queue_summary(summary)
        return summary

    def _count(self, turn: _TurnState, outcome: str, seconds: float) -> None:
        for counts in (
            self._bucket.hosts["startup" if turn.startup else "steady"],
            self._bucket.worker_counts(turn.worker),
        ):
            counts[outcome] += 1
            counts["seconds_sum"] += seconds
            counts["seconds_max"] = max(counts["seconds_max"], seconds)
            counts["histogram"][duration_bucket(seconds)] += 1

    def _summary(
        self,
        turn: _TurnState,
        outcome: str,
        code: str,
        seconds: float,
        stages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ranked = sorted(stages, key=lambda s: float(s.get("seconds") or 0.0), reverse=True)
        kept = ranked[:SUMMARY_MAX_STAGES]
        summary = {
            "event": "turn",
            "schema": 1,
            "process_id": self.process_id,
            "turn_id": turn.turn_id,
            "worker": turn.worker,
            "startup": turn.startup,
            "outcome": outcome,
            "code": str(code or ""),
            "started_at": _utc(turn.started_at),
            "seconds": round(seconds, 3),
            "executor": {
                "calls": turn.executor_calls,
                "wait_seconds_sum": round(turn.executor_wait_seconds, 3),
                "wait_seconds_max": round(turn.executor_wait_max_seconds, 3),
                "run_seconds_sum": round(turn.executor_run_seconds, 3),
            },
            "requests": list(turn.requests),
            "omitted_requests": turn.omitted_requests,
            "stages": kept,
            "omitted_stages": len(ranked) - len(kept),
            "truncated": False,
        }
        # The byte bound counts the whole encoded line; drop the shortest
        # stages, then requests, until it fits, and say so.
        while _encoded_size(TURN_SUMMARY_MESSAGE, summary) > SUMMARY_MAX_BYTES and summary["stages"]:
            summary["stages"] = summary["stages"][:-1]
            summary["omitted_stages"] += 1
            summary["truncated"] = True
        while _encoded_size(TURN_SUMMARY_MESSAGE, summary) > SUMMARY_MAX_BYTES and summary["requests"]:
            summary["requests"] = summary["requests"][:-1]
            summary["omitted_requests"] += 1
            summary["truncated"] = True
        if _encoded_size(TURN_SUMMARY_MESSAGE, summary) > SUMMARY_MAX_BYTES:
            summary["worker"] = summary["worker"][:64]
            summary["code"] = summary["code"][:64]
            summary["truncated"] = True
        return summary

    def _queue_summary(self, summary: dict[str, Any]) -> None:
        line = TURN_SUMMARY_MESSAGE + json.dumps(
            summary, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        )
        size = len(line.encode("utf-8")) + LOG_PREFIX_RESERVE_BYTES
        now = self._monotonic()
        if now - self._rate_window_started >= 60.0:
            self._rate_window_started = now
            self._rate_window_bytes = 0
        if self._rate_window_bytes + size > SUMMARY_BYTES_PER_MINUTE:
            self._bucket.summaries_dropped_rate += 1
            return
        if self._pending_bytes + size > ACCOUNTING_RETAINED_BYTES:
            self._bucket.summaries_dropped_pending += 1
            return
        self._rate_window_bytes += size
        self._bucket.summaries_queued += 1
        self._pending.append(("summary", 0, line, size))
        self._pending_bytes += size

    # Jobs and requests -------------------------------------------------
    def record_job(self, kind: str, event: str, seconds: float | None = None, *, phase: str = "") -> None:
        kind = kind if kind in JOB_KINDS else "other"
        if event not in JOB_EVENTS:
            return
        job = self._bucket.job(kind)
        job[event] += 1
        if seconds is not None and phase in ("queue_wait", "run"):
            job[f"{phase}_histogram"][duration_bucket(max(0.0, seconds))] += 1

    # Buckets -----------------------------------------------------------
    def roll_due(self, *, final: bool = False) -> bool:
        """Close the bucket when its 900 s passed (or at shutdown) and queue it."""

        now = self._monotonic()
        bucket = self._bucket
        if not final and now - bucket.started_monotonic < ACCOUNTING_BUCKET_SECONDS:
            return False
        payload = bucket.payload(
            process_id=self.process_id,
            ended_monotonic=now,
            ended_at=self._wall_clock(),
            final=final,
        )
        payload["in_flight_turns"] = len(self._active)
        payload["buckets_lost_unwritten"] = self.buckets_lost_unwritten
        if self._writer is not None:
            payload["writer"] = dict(self._writer.receipts)
        line = ACCOUNTING_MESSAGE + json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        size = len(line.encode("utf-8")) + LOG_PREFIX_RESERVE_BYTES
        self._queue_bucket(bucket.seq, line, size)
        self._bucket = self._open_bucket()
        return True

    def _queue_bucket(self, seq: int, line: str, size: int) -> None:
        self._pending.append(("bucket", seq, line, size))
        self._pending_bytes += size
        self._pending_buckets += 1
        # Over budget: the oldest unwritten line goes first. A bucket lost
        # this way is counted once; a summary was already counted as queued.
        while (
            self._pending_buckets > ACCOUNTING_RETAINED_BUCKETS
            or self._pending_bytes > ACCOUNTING_RETAINED_BYTES
        ) and len(self._pending) > 1:
            kind, _seq, _line, lost_size = self._pending.popleft()
            self._pending_bytes -= lost_size
            if kind == "bucket":
                self._pending_buckets -= 1
                self.buckets_lost_unwritten += 1
            else:
                self._bucket.summaries_dropped_pending += 1

    def announce_process_start(self) -> None:
        if self._announced:
            return
        self._announced = True
        payload = {
            "event": "process_start",
            "schema": 1,
            "process_id": self.process_id,
            "started_at": _utc(self._wall_clock()),
            # This process cannot see what the previous one left unwritten.
            "prior_coverage": "uncertain",
        }
        line = ACCOUNTING_MESSAGE + json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self._pending.appendleft(("start", 0, line, len(line.encode("utf-8")) + LOG_PREFIX_RESERVE_BYTES))
        self._pending_bytes += self._pending[0][3]

    def abandon_in_flight(self) -> int:
        """At a clean relay stop: count each still-running turn once, as abandoned."""

        abandoned = 0
        for turn in list(self._active.values()):
            turn.finished = True
            self._active.pop(turn.turn_id, None)
            self._count(turn, "abandoned_at_shutdown", max(0.0, self._monotonic() - turn.started_monotonic))
            abandoned += 1
        return abandoned

    async def flush(self, *, max_writes: int = 8) -> dict[str, int]:
        """Hand queued lines to the writer, one at a time, never waiting on a stuck one.

        A line leaves the queue when it is handed over, before the wait: if
        this flush is cancelled while waiting, the line is the writer's
        in-flight write and is reconciled once, never handed over twice.
        """

        written = {"appended": 0, "failed": 0, "unknown": 0, "busy": 0, "dropped": 0}
        if self._writer is None:
            return written
        for _ in range(max(0, max_writes)):
            if not self._pending:
                break
            if self._writer.busy():
                written["busy"] += 1
                break
            item = self._pending[0]
            self._pop_pending()
            kind, seq, line, _size = item
            receipt = await self._writer.write((kind, seq), line)
            written[receipt] += 1
            if receipt == "busy":
                self._restore_pending(item)
                break
            if receipt in ("failed", "dropped") and kind == "bucket":
                # Not appended: this bucket's counts are lost, counted once here.
                self.buckets_lost_unwritten += 1
            if receipt == "unknown":
                break
        return written

    def _restore_pending(self, item: tuple[str, int, str, int]) -> None:
        kind, _seq, _line, size = item
        self._pending.appendleft(item)
        self._pending_bytes += size
        if kind == "bucket":
            self._pending_buckets += 1

    def _pop_pending(self) -> None:
        kind, _seq, _line, size = self._pending.popleft()
        self._pending_bytes -= size
        if kind == "bucket":
            self._pending_buckets -= 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "bucket_seq": self._bucket.seq,
            "in_flight_turns": len(self._active),
            "pending_lines": len(self._pending),
            "pending_bytes": self._pending_bytes,
            "pending_buckets": self._pending_buckets,
            "buckets_lost_unwritten": self.buckets_lost_unwritten,
            "writer": dict(self._writer.receipts) if self._writer is not None else None,
        }


class RelayActivityTrace:
    """Track active and recent relay work without retaining request payloads."""

    def __init__(
        self,
        *,
        slow_seconds: float = SLOW_RELAY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        log: logging.Logger | None = None,
        writer: DiagnosticWriter | None = None,
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
        self.watchdog = LoopStallWatchdog(log=self._log)
        # W461: per-turn and per-job accounting of this relay process. The
        # writer is supplied by the relay (its rotating log); without one the
        # counts stay in memory and nothing is written.
        self.accounting = TurnAccounting(
            writer=writer,
            slow_seconds=self.slow_seconds,
            monotonic=monotonic,
            wall_clock=wall_clock,
        )

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
            self.watchdog.beat()
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
        turn = _TURN.get()
        record = {
            "cycle": self._active_cycle,
            "stage": str(stage or "unknown"),
            "channel": str(channel or ""),
            "operation": str(operation or ""),
            "started_monotonic": self._monotonic(),
            "started_at": self._wall_clock(),
            # Task-local, never inferred: a stage belongs to the turn whose
            # task (or a task it created) ran it. A child that outlives its
            # turn names it only as its origin.
            "turn_id": turn.turn_id if turn is not None and not turn.finished else "",
            "origin_turn_id": turn.turn_id if turn is not None and turn.finished else "",
            "request_id": _REQUEST.get(),
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

    # W461 turn accounting ----------------------------------------------
    def begin_turn(self, worker: str) -> _TurnState:
        """Start one channel turn's context; call before creating its tasks."""

        return self.accounting.begin_turn(worker)

    def end_turn(self, turn: _TurnState, outcome: str, *, code: str = "") -> dict[str, Any] | None:
        """Count the turn once; queue a summary when it was slow, failed or cancelled."""

        return self.accounting.end_turn(
            turn, outcome, code=code, stages=self._turn_stages(turn.turn_id)
        )

    def request_scope(self) -> RequestScope:
        return RequestScope()

    @asynccontextmanager
    async def timed_lock(
        self,
        lock: asyncio.Lock,
        stage: str,
        *,
        channel: str = "",
        operation: str = "",
    ) -> AsyncIterator[None]:
        """``async with lock``, with the wait for it recorded as its own stage."""

        with self.stage(stage, channel=channel, operation=operation):
            await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def record_job(self, kind: str, event: str, seconds: float | None = None, *, phase: str = "") -> None:
        self.accounting.record_job(kind, event, seconds, phase=phase)

    async def flush_accounting(self) -> dict[str, int]:
        """Close a due bucket and hand queued lines to the writer; never waits on a stuck write."""

        self.accounting.roll_due()
        return await self.accounting.flush()

    async def close_accounting(self) -> None:
        """At a clean relay stop: count running turns as abandoned, write the final bucket once."""

        self.accounting.abandon_in_flight()
        self.accounting.roll_due(final=True)
        await self.accounting.flush()
        if self.accounting._writer is not None:
            self.accounting._writer.close()

    def _turn_stages(self, turn_id: str) -> list[dict[str, Any]]:
        ended_at = self._wall_clock()
        records = [r for r in self._history if r.get("turn_id") == turn_id]
        records.extend(
            {
                **r,
                "ended_at": ended_at,
                "seconds": max(0.0, ended_at - float(r["started_at"])),
                "outcome": "in_progress",
            }
            for r in self._active.values()
            if r.get("turn_id") == turn_id
        )
        return [
            {
                "stage": allowlisted(str(r.get("stage") or ""), STAGE_NAMES),
                "operation": allowlisted(str(r.get("operation") or ""), OPERATION_NAMES),
                "request_id": str(r.get("request_id") or ""),
                "seconds": round(float(r.get("seconds") or 0.0), 3),
                "outcome": (
                    str(r.get("outcome") or "unknown")
                    if str(r.get("outcome") or "").split(":", 1)[0]
                    in ("succeeded", "cancelled", "in_progress")
                    else "failed:" + allowlisted(
                        str(r.get("outcome") or "").split(":", 1)[-1], ERROR_CLASSES
                    )
                ),
            }
            for r in records
        ]

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
    "DiagnosticWriter",
    "LoopStallWatchdog",
    "RequestScope",
    "TurnAccounting",
    "RelayActivityTrace",
    "SLOW_RELAY_SECONDS",
    "major_faults",
    "memory_log_fields",
    "process_memory",
]

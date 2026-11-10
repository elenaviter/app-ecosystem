"""Loss-aware wake timing through the relay's existing non-blocking log owner.

This observer never reads a mailbox, quota or native queue. It keeps no payloads,
creates no thread or timer, and cannot authorize or postpone delivery.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import re
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

MESSAGE = "Problem Board wake trace v1 "
MAX_LINE_BYTES = 2048  # includes the existing log formatter's prefix reserve
PREFIX_RESERVE_BYTES = 128
BYTES_PER_MINUTE = 64 * 1024
MAX_SIGNATURES = 256
REPEAT_SECONDS = 60.0
LOCK_WAIT_BOUNDS = (1.0, 5.0, 30.0, 60.0)
MAX_REFS = 8
_WORKER = re.compile(r"codex-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z\Z")
_CHECK: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "problem_board_wake_trace_check", default=None
)
EVENTS = frozenset({
    "control_received", "mail_materialized", "mail_replayed", "quota_read_started", "quota_read_finished",
    "quota_cache", "decision", "queue_started", "queue_finished", "notify_lock",
    "ack_observed", "check_finished",
})
REASONS = frozenset({
    "empty", "detached", "quiet", "quota_error", "quota_hold", "session_watch",
    "queue_withheld", "eligible", "coalesced", "queued", "unconfirmed", "failed",
    "read_ok", "read_error", "read_unmeasured", "cache_ok", "cache_error",
    "acknowledged", "already_acknowledged", "stale", "succeeded", "cancelled", "error",
    "no_listener", "after_quota_empty", "wake_still_queued", "wake_consumed_retry_exhausted",
    "normal", "beside_turn", "local_store_returned", "replayed_store_returned",
})
ERRORS = frozenset({
    "work_codex_quota_account_missing", "work_codex_quota_command_missing",
    "work_codex_quota_account_mismatch", "work_codex_quota_session_mismatch",
    "work_codex_quota_timeout", "work_codex_quota_unavailable", "work_codex_quota_unmeasured",
    "field_worker_not_listening", "field_worker_wake_conflict",
    "field_worker_wake_already_acknowledged", "agent_rate_limited",
    "codex_queue_failed", "codex_command_not_found", "TimeoutExpired", "OSError",
})


def tag(value: Any) -> str:
    """A correlation tag, never a ref/slug, account identifier or authority."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(value: Any) -> str:
    if isinstance(value, str) and _STAMP.fullmatch(value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    return ""


def _fields(values: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in ("reason", "error_code", "kind", "source"):
        value = values.get(key)
        if value:
            allowed = (REASONS if key == "reason" else ERRORS if key == "error_code"
                       else {"ok", "unknown", "rate_limited", "out_of_tokens"} if key == "kind"
                       else {"codex-app-server", "codex-rollout"})
            clean[key] = value if isinstance(value, str) and value in allowed else "other"
    for key in ("wake_id", "submission_id", "message_ref", "source_message_ref", "control_ref"):
        if value := tag(values.get(key)):
            clean[key + "_sha256"] = value
    refs = values.get("message_refs")
    if isinstance(refs, (list, tuple)):
        clean["message_refs_sha256"] = [value for ref in refs[:MAX_REFS] if (value := tag(ref))]
        clean["message_refs_omitted"] = max(0, len(refs) - MAX_REFS)
    for key in ("pending", "message_count"):
        value = values.get(key)
        if type(value) is int:
            clean[key] = max(0, min(value, 1_000_000))
    for key in ("retried", "mailbox_read", "admission_seen"):
        if type(values.get(key)) is bool:
            clean[key] = values[key]
    status = values.get("delivery_status")
    if isinstance(status, str) and status in {"pending", "ignored_sender_limbo", "ignored_recipient_limbo"}:
        clean["delivery_status"] = status
    for key in ("seconds", "quota_age_seconds"):
        value = values.get(key)
        if type(value) in (float, int) and math.isfinite(value):
            clean[key] = round(max(0.0, min(float(value), 86400.0)), 4)
    for key in ("remote_created_at", "recorded_at", "retry_at", "quota_observed_at"):
        if value := _stamp(values.get(key)):
            clean[key] = value
    return clean


class WakeTrace:
    """Bound bytes/state and repeated observations before handing a line off.

    ``submit`` is the existing relay log owner's non-blocking ``submit_line``.
    None means no owner/disabled. A returned None means owner queue refusal.
    Accepted records carry a Future for the eventual write result. Failed or
    cancelled receipts increment the loss count for subsequent records.
    """

    def __init__(self, *, submit: Callable[[str], Any] | None, process_id: str,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._submit = submit
        self._process_id = process_id
        self._monotonic, self._wall_clock = monotonic, wall_clock
        self._lock = threading.Lock()
        self._window = monotonic()
        self._bytes = self._seq = self._checks = 0
        self._seen: OrderedDict[tuple[str, str, str, int], tuple[str, float]] = OrderedDict()
        self.losses = {"budget": 0, "owner_refused": 0, "submit_failed": 0, "oversize": 0,
                       "construction_failed": 0, "write_failed": 0}

    @contextmanager
    def check(self, worker: str) -> Iterator[None]:
        token = None
        try:
            if self._submit is not None and isinstance(worker, str) and _WORKER.fullmatch(worker):
                with self._lock:
                    self._checks += 1
                    number = self._checks
                token = _CHECK.set({"worker": worker, "id": f"{self._process_id}:{number}",
                                    "started": self._monotonic()})
        except Exception:  # noqa: BLE001 - even a failed observer cannot fail a check
            with self._lock:
                self.losses["construction_failed"] += 1
        try:
            yield
        finally:
            if token is not None:
                _CHECK.reset(token)

    def emit(self, event: str, worker: str, *, repeat: bool = False, **values: Any) -> bool:
        # Observation failures never change the original exception or result.
        try:
            return self._emit(event, worker, repeat=repeat, **values)
        except Exception:  # noqa: BLE001 - diagnostics are not delivery
            with self._lock:
                self.losses["construction_failed"] += 1
            return False

    def _emit(self, event: str, worker: str, *, repeat: bool, **values: Any) -> bool:
        if self._submit is None or event not in EVENTS or not _WORKER.fullmatch(worker):
            return False
        now = self._monotonic()
        fields = _fields(values)
        # Duration/clock movement must not turn unchanged holds into poll spam.
        signature = json.dumps({k: v for k, v in fields.items()
                                if k not in {"seconds", "quota_age_seconds", "retry_at",
                                             "quota_observed_at", "recorded_at"}}, sort_keys=True)
        key = (worker, event, "", 0)
        if event == "notify_lock":
            # Sample each range independently so routine acquisitions cannot
            # suppress contention, even when short and long waits alternate.
            band = bisect_right(LOCK_WAIT_BOUNDS, fields.get("seconds", 0.0))
            key = (worker, event, fields.get("reason", ""), band)
        with self._lock:
            previous = self._seen.get(key)
            if repeat and previous and previous[0] == signature and now - previous[1] < REPEAT_SECONDS:
                return False
            if now - self._window >= 60.0:
                self._window, self._bytes = now, 0
            self._seq += 1
            row = {"schema": 1, "process_id": self._process_id, "seq": self._seq,
                   "event": event, "worker": worker,
                   "observed_at": datetime.fromtimestamp(self._wall_clock(), timezone.utc)
                   .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                   "prior_losses": dict(self.losses), **fields}
            if stamp := fields.get("quota_observed_at"):
                age = self._wall_clock() - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                row["quota_age_seconds"] = round(max(0.0, min(age, 86400.0)), 4)
            check = _CHECK.get()
            if check is not None and check["worker"] == worker:
                row["check_id"] = check["id"]
                row["check_seconds"] = round(max(0.0, now - check["started"]), 4)
            line = MESSAGE + json.dumps(row, separators=(",", ":"), sort_keys=True)
            size = len(line.encode("utf-8")) + PREFIX_RESERVE_BYTES
            if size > MAX_LINE_BYTES:
                self.losses["oversize"] += 1
                return False
            if self._bytes + size > BYTES_PER_MINUTE:
                self.losses["budget"] += 1
                return False
            self._bytes += size
            self._seen[key] = (signature, now)
            self._seen.move_to_end(key)
            while len(self._seen) > MAX_SIGNATURES:
                self._seen.popitem(last=False)
        # No lock is held across the owner's bounded enqueue. No wait/retry.
        try:
            receipt = self._submit(line)
        except Exception:  # noqa: BLE001 - never copy sink exception text
            with self._lock:
                self.losses["submit_failed"] += 1
            return False
        if receipt is None:
            with self._lock:
                self.losses["owner_refused"] += 1
            return False
        if isinstance(receipt, Future):
            receipt.add_done_callback(self._write_finished)
        return True

    def _write_finished(self, receipt: Future[None]) -> None:
        # The owner invokes this after completion. Keep only the count.
        if receipt.cancelled() or receipt.exception() is not None:
            with self._lock:
                self.losses["write_failed"] += 1


def record(trace: Any, event: str, worker: str, **fields: Any) -> None:
    """Also accept older test adapters with no wake observer."""
    try:
        observer = getattr(trace, "wakes", None)
        if isinstance(observer, WakeTrace):
            observer.emit(event, worker, **fields)
    except Exception:  # noqa: BLE001 - observation is not a new delivery failure
        pass

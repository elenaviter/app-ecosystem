"""Notification-only event stream for a Claude Code worker watch."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from project_board.contract.errors import DomainError

from .local_wake import WorkerWakeListener
from .session import probe_worker_input
from .store import SharedFieldStore


def _availability(result: dict[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
    pending_refs = tuple(str(item) for item in result.get("pending_refs") or [])
    held_refs = tuple(str(item) for item in result.get("held_lease_refs") or [])
    held_count = int(result.get("held_lease_count") or 0)
    signals = tuple(
        json.dumps(item, sort_keys=True) for item in result.get("signals") or []
    )
    held_signature = (f"held:{held_count}",) if held_count else ()
    signature = pending_refs + held_refs + held_signature + signals
    event = {
        "event": "problem_board.inbox_available",
        "pending_count": int(result.get("pending_count") or 0),
        "held_lease_count": held_count,
        "signals": list(result.get("signals") or []),
        "instruction": str(result.get("instruction") or ""),
    }
    return signature, event


def worker_watch_events(
    field: SharedFieldStore,
    *,
    worker_name: str,
    check_interval_seconds: int,
    coalesce_seconds: float,
) -> Iterator[dict[str, Any]]:
    """Yield availability while local writes wake probes ahead of the timer."""

    interval = max(5, min(int(check_interval_seconds), 300))
    coalesce = max(0.0, min(float(coalesce_seconds), 5.0))
    last_signature: tuple[str, ...] = ()
    failure_signature = ""
    failure_delay = 5

    # Bind before the first probe. A write before bind is found by that probe;
    # a write after bind also leaves a datagram, so neither restart order has a
    # gap between observing the mailbox and waiting for its next change.
    with WorkerWakeListener(field.root, worker_name) as wake:
        while True:
            try:
                result = probe_worker_input(field, worker_name=worker_name)
                signature, event = _availability(result)
                if signature and signature != last_signature:
                    if result.get("pending_refs") and coalesce:
                        deadline = time.monotonic() + coalesce
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            # A datagram drains the current burst but does not
                            # shorten its fixed coalescing window.
                            wake.wait(remaining)
                        result = probe_worker_input(field, worker_name=worker_name)
                        signature, event = _availability(result)
                    last_signature = signature
                    yield {**event, "worker": worker_name}
                elif not signature:
                    last_signature = ()

                if failure_signature:
                    yield {
                        "event": "problem_board.watch_recovered",
                        "worker": worker_name,
                    }
                failure_signature = ""
                failure_delay = 5
                wake.wait(interval)
            except (DomainError, OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
                signature = f"{code}:{exc}"
                if signature != failure_signature:
                    yield {
                        "event": "problem_board.watch_failed",
                        "worker": worker_name,
                        "code": code,
                        "message": str(exc),
                        "retry_seconds": failure_delay,
                    }
                failure_signature = signature
                wake.wait(failure_delay)
                failure_delay = min(failure_delay * 2, 60)

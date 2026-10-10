"""One trace id per Card-save request, logged at every Connection Hub hop (operator, 2026-10-10).

Operator, ~11:40 Berlin: "logs. we must have logs that trace request end to end". The trace is the
DIGEST of the request's own id: the UI request's audit id for the save, the plan's own request id for
the PLAN hops (the project logs the pairing of the two digests when it posts the PLAN). Each line: the
trace digest, a hop name, entry/ok/refused/error, an exact enumerated Hub code (else "other") and
elapsed ms. Never an id itself, a body, key, signature, token, subject or other value.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import re
import time
from typing import Any, Iterator

LOGGER = logging.getLogger("connection_hub.card_save_trace")

_TRACE: contextvars.ContextVar[str] = contextvars.ContextVar("hub_card_save_trace", default="-")
_HOP = re.compile(r"[a-z][a-z0-9_.]{0,63}")


def safe_id(value: Any) -> str:
    """ALWAYS a deterministic short digest of the id, never the id itself (#709 review); the project
    logs the same digest of the same id, so both sides join."""
    if type(value) is not str or not value:
        return "-"
    return "h:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def fixed_code(value: Any) -> str:
    """An exact enumerated Hub code (card_save_codes), else "other"; never a pattern match."""
    from .card_save_codes import CARD_SAVE_CODES
    if value is None:
        return "-"
    return value if type(value) is str and value in CARD_SAVE_CODES else "other"


@contextlib.contextmanager
def trace_scope(request_id: Any) -> Iterator[str]:
    """Bind this request's trace id; an outer trace on the same task is kept, not replaced."""
    outer = _TRACE.get()
    trace = outer if outer != "-" else safe_id(request_id)
    token = _TRACE.set(trace)
    try:
        yield trace
    finally:
        _TRACE.reset(token)


def hop(name: str, outcome: str, *, code: Any = None, started: float | None = None, **refs: Any) -> None:
    if not _HOP.fullmatch(name) or outcome not in {"entry", "ok", "refused", "error"}:
        return
    elapsed = (time.monotonic() - started) * 1000.0 if started is not None else 0.0
    extra = "".join(f" {key}={safe_id(value)}" for key, value in sorted(refs.items())
                    if value is not None and _HOP.fullmatch(key))
    LOGGER.info("card-save trace=%s hop=%s outcome=%s code=%s ms=%.1f%s",
                _TRACE.get(), name, outcome, fixed_code(code), elapsed, extra)


def answer_outcome(answer: Any) -> tuple[str, Any]:
    """ok/refused and the fixed code of a Hub answer ({ok, error{code}} or a signed plan answer)."""
    if not isinstance(answer, dict):
        return "error", None
    if answer.get("ok") is not False:
        return "ok", None
    error = answer.get("error")
    if isinstance(error, dict):
        return "refused", error.get("code")
    result = (answer.get("plan_answer") or {}).get("result") if isinstance(answer.get("plan_answer"), dict) else None
    return "refused", result.get("code") if isinstance(result, dict) else error


__all__ = ["answer_outcome", "fixed_code", "hop", "safe_id", "trace_scope"]

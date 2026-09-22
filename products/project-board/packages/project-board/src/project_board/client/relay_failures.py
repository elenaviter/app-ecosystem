from __future__ import annotations

import asyncio
import errno
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..contract.errors import DomainError


class RelayStageError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any],
        retryable: bool,
    ) -> None:
        super().__init__(code, message, status=503 if retryable else 500, details=details)
        self.retryable = retryable


def safe_target(target: str) -> str:
    value = str(target or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme:
        authority = parsed.netloc.rsplit("@", 1)[-1]
        value = urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    return value[:512]


def is_descriptor_exhaustion(error: BaseException) -> bool:
    """EMFILE (this process) or ENFILE (the host): no descriptor to open with."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in (errno.EMFILE, errno.ENFILE):
            return True
        current = current.__cause__ or current.__context__
    return False


def descriptor_limit() -> int | None:
    """The soft RLIMIT_NOFILE of this process, or None where unavailable."""

    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft)
    except (ImportError, OSError, ValueError):
        return None


def descriptor_limit_label() -> str:
    value = descriptor_limit()
    return str(value) if value is not None else "unknown"


def is_timeout_error(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, asyncio.TimeoutError)) or (
        type(error).__name__ == "TimeoutError"
        and type(error).__module__.startswith("socketio")
    )


def staged_failure(
    error: Exception,
    *,
    operation: str,
    target: str,
    elapsed_seconds: float,
    retryable: bool,
) -> Exception:
    """Give an unstructured relay exception the context lost at its await."""

    if isinstance(error, RelayStageError):
        return error
    if isinstance(error, DomainError) and error.code and str(error).strip():
        return error
    original_code = str(getattr(error, "code", "") or "").strip()
    if original_code and str(error).strip():
        return error
    error_type = type(error).__name__
    is_timeout = is_timeout_error(error)
    is_connection = isinstance(error, (ConnectionError, OSError)) or (
        error_type == "ConnectionError"
        and type(error).__module__.startswith("socketio")
    )
    exhausted = is_descriptor_exhaustion(error)
    code = original_code or (
        "work_relay_descriptor_exhausted"
        if exhausted
        else "work_relay_channel_timeout"
        if is_timeout
        else "work_relay_channel_connection_failed"
        if is_connection
        else "work_relay_channel_operation_failed"
    )
    target_label = safe_target(target)
    elapsed = round(max(0.0, float(elapsed_seconds)), 3)
    action = "timed out" if is_timeout else "failed"
    message = f"{operation} against {target_label} {action} after {elapsed:.3f}s."
    if exhausted:
        # Errno 24 arrives through the same socket calls a network failure
        # does and used to be reported as one; only the sub-quarter-second
        # elapsed time hinted at the difference. Say what it is and
        # what the ceiling was.
        message = (
            f"{operation} against {target_label} could not open a file or socket: "
            f"the relay process has no free file descriptors under its limit of "
            f"{descriptor_limit_label()} (Errno 24, not a network failure). "
            f"Elapsed {elapsed:.3f}s."
        )
    details = {
        "operation": operation,
        "target": target_label,
        "elapsed_seconds": elapsed,
        "cause_type": error_type,
        "retryable": bool(retryable),
    }
    if exhausted:
        details["descriptor_limit"] = descriptor_limit()
    return RelayStageError(code, message, details=details, retryable=retryable)


def failure_type(error: BaseException) -> str:
    details = getattr(error, "details", None)
    if isinstance(details, Mapping) and details.get("cause_type"):
        return str(details["cause_type"])
    return type(error).__name__


def failure_message(error: BaseException) -> str:
    return str(error).strip() or (
        f"Relay channel failed with {getattr(error, 'code', '') or failure_type(error)}."
    )

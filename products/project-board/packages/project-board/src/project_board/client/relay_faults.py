from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from .io import (
    atomic_write_json,
    component,
    exclusive_lock,
    new_id,
    parse_utc,
    read_json,
    utc_now,
)


logger = logging.getLogger(__name__)

RELAY_FAULT_SCHEMA = "problem-board.relay-fault.v1"
DEFAULT_RELAY_FAULT_TTL_SECONDS = 300
MAX_RELAY_FAULT_TTL_SECONDS = 900

# A host operator may exercise only failures the relay already classifies as
# retryable. The switch is not a general exception-injection surface.
INJECTABLE_RELAY_FAULT_CODES = frozenset(
    {
        "data_bus_outcome_unknown",
        "oauth_metadata_request_failed",
        "oauth_profile_lock_timeout",
        "oauth_server_metadata_unavailable",
        "oauth_session_lock_timeout",
        "state_lock_timeout",
        "work_mcp_tool_failed",
        "work_relay_stream_expired",
        "work_relay_transport_unavailable",
    }
)


def _fault_root(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve().parent / "relay-faults"


def _fault_path(config_path: str | Path, worker_name: str) -> Path:
    name = component(worker_name, field="worker_name")
    return _fault_root(config_path) / f"{name}.json"


def _lock_path(config_path: str | Path) -> Path:
    return _fault_root(config_path) / ".lock"


def _validated_record(
    value: Mapping[str, Any],
    *,
    worker_name: str,
) -> dict[str, Any]:
    if value.get("schema") != RELAY_FAULT_SCHEMA:
        raise DomainError(
            "work_relay_fault_record_invalid",
            "The host-local relay fault record has an unsupported schema.",
        )
    if str(value.get("worker_name") or "") != worker_name:
        raise DomainError(
            "work_relay_fault_record_invalid",
            "The host-local relay fault record does not match its worker.",
        )
    component(value.get("fault_id"), field="fault_id")
    code = str(value.get("code") or "")
    if code not in INJECTABLE_RELAY_FAULT_CODES:
        raise DomainError(
            "work_relay_fault_code_invalid",
            "The host-local relay fault record names a non-retryable code.",
            details={"code": code},
        )
    if value.get("state") != "armed":
        raise DomainError(
            "work_relay_fault_record_invalid",
            "The host-local relay fault record is not armed.",
        )
    parse_utc(str(value.get("armed_at") or ""))
    parse_utc(str(value.get("expires_at") or ""))
    return dict(value)


def _is_expired(record: Mapping[str, Any]) -> bool:
    return parse_utc(str(record.get("expires_at") or "")) <= datetime.now(
        timezone.utc
    )


def inject_relay_fault(
    config_path: str | Path,
    *,
    worker_name: str,
    code: str,
    ttl_seconds: int = DEFAULT_RELAY_FAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Arm one bounded channel-open failure in machine-local operator state."""

    normalized_worker = component(worker_name, field="worker_name")
    normalized_code = component(code, field="code")
    if normalized_code not in INJECTABLE_RELAY_FAULT_CODES:
        raise DomainError(
            "work_relay_fault_code_invalid",
            "Relay fault injection accepts only known retryable relay error codes.",
            details={
                "code": normalized_code,
                "allowed_codes": sorted(INJECTABLE_RELAY_FAULT_CODES),
            },
        )
    seconds = int(ttl_seconds)
    if not 5 <= seconds <= MAX_RELAY_FAULT_TTL_SECONDS:
        raise DomainError(
            "work_relay_fault_ttl_invalid",
            "Relay fault expiry must be between 5 and "
            f"{MAX_RELAY_FAULT_TTL_SECONDS} seconds.",
        )
    now = datetime.now(timezone.utc)
    record = {
        "schema": RELAY_FAULT_SCHEMA,
        "fault_id": new_id("relay_fault"),
        "worker_name": normalized_worker,
        "code": normalized_code,
        "armed_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z"),
        "state": "armed",
    }
    path = _fault_path(config_path, normalized_worker)
    with exclusive_lock(_lock_path(config_path)):
        existing = read_json(path, required=False)
        if existing:
            try:
                existing = _validated_record(
                    existing, worker_name=normalized_worker
                )
            except DomainError:
                existing = {}
            if existing and not _is_expired(existing):
                raise DomainError(
                    "work_relay_fault_already_armed",
                    "This worker already has an unconsumed relay fault.",
                    status=409,
                    details={"fault_id": str(existing.get("fault_id") or "")},
                )
        atomic_write_json(path, record)
    return record


def clear_relay_fault(
    config_path: str | Path,
    *,
    worker_name: str,
) -> bool:
    path = _fault_path(config_path, worker_name)
    with exclusive_lock(_lock_path(config_path)):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def pending_relay_faults(
    config_path: str | Path,
    *,
    worker_names: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    selected = (
        {component(item, field="worker_name") for item in worker_names}
        if worker_names is not None
        else None
    )
    root = _fault_root(config_path)
    records: list[dict[str, Any]] = []
    with exclusive_lock(_lock_path(config_path)):
        for path in sorted(root.glob("*.json")):
            worker_name = path.stem
            if selected is not None and worker_name not in selected:
                continue
            try:
                record = _validated_record(
                    read_json(path), worker_name=worker_name
                )
            except DomainError:
                logger.warning(
                    "Discarding an invalid host-local relay fault record path=%s",
                    path,
                    exc_info=True,
                )
                path.unlink(missing_ok=True)
                continue
            if _is_expired(record):
                path.unlink(missing_ok=True)
                continue
            records.append(record)
    return records


def consume_relay_fault(
    config_path: str | Path,
    *,
    worker_name: str,
) -> dict[str, Any] | None:
    """Remove and return one fault before the relay raises it."""

    path = _fault_path(config_path, worker_name)
    with exclusive_lock(_lock_path(config_path)):
        try:
            record = _validated_record(
                read_json(path),
                worker_name=component(worker_name, field="worker_name"),
            )
        except DomainError as exc:
            if exc.code == "field_record_not_found":
                return None
            logger.warning(
                "Discarding an invalid host-local relay fault record worker=%s",
                worker_name,
                exc_info=True,
            )
            path.unlink(missing_ok=True)
            return None
        if _is_expired(record):
            path.unlink(missing_ok=True)
            return None
        path.unlink()
    return {
        **record,
        "state": "consumed",
        "consumed_at": utc_now(),
    }


__all__ = [
    "DEFAULT_RELAY_FAULT_TTL_SECONDS",
    "INJECTABLE_RELAY_FAULT_CODES",
    "MAX_RELAY_FAULT_TTL_SECONDS",
    "RELAY_FAULT_SCHEMA",
    "clear_relay_fault",
    "consume_relay_fault",
    "inject_relay_fault",
    "pending_relay_faults",
]

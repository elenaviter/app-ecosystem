from __future__ import annotations

from typing import Any, Mapping

from ..contract.errors import DomainError
from .host_config import HostRelayConfig
from .store import RELAY_DIAGNOSTIC_SCHEMA, SharedFieldStore, bounded_relay_diagnostic

# W553, operator 2026-10-05: "when i do ~/.local/bin/pb host inspect I get just
# huge amount of something. i see attempts in this output. why its there? its
# huge! if the agents read it they spend a lot of tokens on only reading this."
# The default view is one bounded line of facts per channel; the stored
# intervals and their attempts only on request.
FULL_DIAGNOSTICS_COMMAND = ["pb", "host", "inspect", "--diagnostics"]
_SUMMARY_MESSAGE_CHARS = 300


def _channel_diagnostic(row: Mapping[str, Any] | None) -> dict[str, Any]:
    diagnostic = (row or {}).get("relay_diagnostic")
    if isinstance(diagnostic, Mapping):
        return bounded_relay_diagnostic(diagnostic)
    return {
        "schema": RELAY_DIAGNOSTIC_SCHEMA,
        "state": "not_recorded",
        "code": "",
        "started_at": "",
        "recent": [],
    }


def _short(text: Any) -> str:
    value = str(text or "")
    return value if len(value) <= _SUMMARY_MESSAGE_CHARS else value[: _SUMMARY_MESSAGE_CHARS - 1] + "…"


def _summary(diagnostic: Mapping[str, Any]) -> dict[str, Any]:
    """The channel's state now and its last error with time, nothing per attempt."""

    summary: dict[str, Any] = {
        "schema": diagnostic.get("schema") or RELAY_DIAGNOSTIC_SCHEMA,
        "state": diagnostic.get("state") or "not_recorded",
        "code": diagnostic.get("code") or "",
        "started_at": diagnostic.get("started_at") or "",
    }
    for key in ("last_attempt_at", "failure_attempts", "retryable"):
        if diagnostic.get(key) not in (None, ""):
            summary[key] = diagnostic[key]
    if diagnostic.get("message"):
        summary["message"] = _short(diagnostic["message"])
    recent = [item for item in diagnostic.get("recent") or [] if isinstance(item, Mapping)]
    summary["recent_count"] = len(recent)
    if recent:
        latest = recent[-1]
        summary["latest"] = {
            key: latest[key]
            for key in ("code", "started_at", "ended_at", "retry_outcome", "failure_attempts")
            if latest.get(key) not in (None, "")
        }
        if latest.get("message"):
            summary["latest"]["message"] = _short(latest["message"])
    return summary


def host_relay_diagnostics(config: HostRelayConfig, *, full: bool = False) -> dict[str, Any]:
    """Read the host-local answer available while its remote route is down.

    Only channels the host still serves are shown; a disabled channel belongs
    to a session that is gone (W553). Without ``full``, each channel carries a
    bounded summary; ``full`` returns the stored record within its age bound.
    """

    field = SharedFieldStore(config.field_root)
    channels: list[dict[str, Any]] = []
    disabled = 0
    for channel in config.workers:
        if channel.state == "disabled":
            disabled += 1
            continue
        try:
            row = field.read_worker(channel.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            row = None
        diagnostic = _channel_diagnostic(row)
        channels.append(
            {
                "worker_name": channel.worker_name,
                "worker_alias": channel.worker_alias,
                "channel_state": channel.state,
                "relay_diagnostic": diagnostic if full else _summary(diagnostic),
            }
        )
    result: dict[str, Any] = {
        "transport": field.relay_transport_state(),
        "channels": channels,
        "disabled_channels_not_shown": disabled,
    }
    if not full:
        result["full_diagnostics"] = FULL_DIAGNOSTICS_COMMAND
    return result


__all__ = ["FULL_DIAGNOSTICS_COMMAND", "host_relay_diagnostics"]

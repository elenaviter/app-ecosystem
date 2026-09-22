from __future__ import annotations

from typing import Any, Mapping

from ..contract.errors import DomainError
from .host_config import HostRelayConfig
from .store import RELAY_DIAGNOSTIC_SCHEMA, SharedFieldStore


def _channel_diagnostic(row: Mapping[str, Any] | None) -> dict[str, Any]:
    diagnostic = (row or {}).get("relay_diagnostic")
    if isinstance(diagnostic, Mapping):
        return dict(diagnostic)
    return {
        "schema": RELAY_DIAGNOSTIC_SCHEMA,
        "state": "not_recorded",
        "code": "",
        "started_at": "",
        "recent": [],
    }


def host_relay_diagnostics(config: HostRelayConfig) -> dict[str, Any]:
    """Read the host-local answer available while its remote route is down."""

    field = SharedFieldStore(config.field_root)
    channels: list[dict[str, Any]] = []
    for channel in config.workers:
        try:
            row = field.read_worker(channel.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            row = None
        channels.append(
            {
                "worker_name": channel.worker_name,
                "worker_alias": channel.worker_alias,
                "channel_state": channel.state,
                "relay_diagnostic": _channel_diagnostic(row),
            }
        )
    return {
        "transport": field.relay_transport_state(),
        "channels": channels,
    }


__all__ = ["host_relay_diagnostics"]

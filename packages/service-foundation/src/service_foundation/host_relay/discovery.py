"""Python entry-point discovery for host-relay adapters."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import metadata
from typing import Any

from service_foundation.host_relay.contracts import (
    HostRelayAdapter,
    HostRelayAdapterFactory,
)

HOST_RELAY_ADAPTER_ENTRY_POINT = "service_foundation.host_relay.adapters"


def _entry_points() -> tuple[metadata.EntryPoint, ...]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        selected = discovered.select(group=HOST_RELAY_ADAPTER_ENTRY_POINT)
    else:  # pragma: no cover - Python 3.9 compatibility
        selected = discovered.get(HOST_RELAY_ADAPTER_ENTRY_POINT, ())
    return tuple(selected)


def discover_host_relay_adapters() -> dict[str, metadata.EntryPoint]:
    """Return installed adapter entry points keyed by their stable ids."""

    adapters: dict[str, metadata.EntryPoint] = {}
    for entry_point in _entry_points():
        adapter_id = str(entry_point.name or "").strip().lower()
        if not adapter_id:
            raise ValueError("A host-relay adapter entry point has no name.")
        if adapter_id in adapters:
            raise ValueError(f"Duplicate host-relay adapter entry point: {adapter_id}")
        adapters[adapter_id] = entry_point
    return adapters


def load_host_relay_adapter_factory(adapter_id: str) -> HostRelayAdapterFactory:
    selected = str(adapter_id or "").strip().lower()
    entry_point = discover_host_relay_adapters().get(selected)
    if entry_point is None:
        raise LookupError(f"Host-relay adapter is not installed: {selected or '<empty>'}")
    factory: Any = entry_point.load()
    if not callable(factory):
        raise TypeError(f"Host-relay adapter entry point is not callable: {selected}")
    return factory


def create_host_relay_adapter(
    adapter_id: str,
    config: Mapping[str, Any],
) -> HostRelayAdapter:
    selected = str(adapter_id or "").strip().lower()
    adapter = load_host_relay_adapter_factory(selected)(dict(config))
    if not callable(getattr(adapter, "poll_once", None)):
        raise TypeError(f"Host-relay adapter does not satisfy the runtime contract: {selected}")
    actual_id = str(adapter.adapter_id or "").strip().lower()
    if actual_id != selected:
        raise ValueError(
            f"Host-relay adapter id mismatch: requested {selected!r}, received {actual_id!r}"
        )
    return adapter


__all__ = [
    "HOST_RELAY_ADAPTER_ENTRY_POINT",
    "create_host_relay_adapter",
    "discover_host_relay_adapters",
    "load_host_relay_adapter_factory",
]

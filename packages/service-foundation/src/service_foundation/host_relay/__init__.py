"""Runtime contracts for outbound host relays."""

from service_foundation.host_relay.contracts import (
    HostRelayAdapter,
    HostRelayAdapterFactory,
    HostRelayEvent,
    HostRelayRetryableError,
)
from service_foundation.host_relay.discovery import (
    HOST_RELAY_ADAPTER_ENTRY_POINT,
    create_host_relay_adapter,
    discover_host_relay_adapters,
    load_host_relay_adapter_factory,
)
from service_foundation.host_relay.runtime import (
    HostRelayHealth,
    HostRelayPolicy,
    HostRelayRuntime,
)

__all__ = [
    "HOST_RELAY_ADAPTER_ENTRY_POINT",
    "HostRelayAdapter",
    "HostRelayAdapterFactory",
    "HostRelayEvent",
    "HostRelayHealth",
    "HostRelayPolicy",
    "HostRelayRetryableError",
    "HostRelayRuntime",
    "create_host_relay_adapter",
    "discover_host_relay_adapters",
    "load_host_relay_adapter_factory",
]

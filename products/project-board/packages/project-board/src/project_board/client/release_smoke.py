"""Import the runtime surfaces that must load before a release is activated.

This module belongs to the candidate release. Its imports evolve with that
release's runtime dependencies, so the host installer does not preserve a
retired package family merely to satisfy an older smoke contract.
"""

from __future__ import annotations

from importlib import import_module


RUNTIME_IMPORTS = (
    "project_board.client.cli",
    "project_board.client.relay",
    "project_board.client.authorization",
    "connection_hub.caller.services",
    "connection_hub.caller.paths",
    "connection_hub.caller.profile_connection",
    "service_foundation.host_relay",
    "app_foundation.data_bus",
)


def verify_runtime_imports() -> None:
    for module in RUNTIME_IMPORTS:
        import_module(module)


verify_runtime_imports()


__all__ = ["RUNTIME_IMPORTS", "verify_runtime_imports"]

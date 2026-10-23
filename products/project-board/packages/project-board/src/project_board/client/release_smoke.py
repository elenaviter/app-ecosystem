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


def verify_hashlib() -> None:
    """The candidate's Python builds hashlib's BLAKE2 hashes (W933).

    A Python whose ``_blake2`` cannot load still imports hashlib, logging a
    traceback, so the import smoke alone passes and every pb command of the
    activated release prints two tracebacks. ``pb status`` names the fix.
    """

    from project_board.client.prerequisites import hashlib_state  # noqa: PLC0415

    usable, detail = hashlib_state()
    if not usable:
        raise ImportError(f"hashlib BLAKE2 is unavailable; run `pb status` for the fix: {detail}")


verify_runtime_imports()
verify_hashlib()


__all__ = ["RUNTIME_IMPORTS", "verify_hashlib", "verify_runtime_imports"]

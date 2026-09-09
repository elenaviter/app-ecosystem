# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Process-level Connection Hub public base URL.

The consent payload's ``connection_hub_url`` deep link must be openable
OUTSIDE the app origin — an external MCP agent relays it verbatim to a user
whose browser has no notion of the deployment host. The shared payload
builder therefore prefixes the deployment's public base URL, and every
surface (chat, API, MCP) agrees because they share the builder.

Source of truth: the Connection Hub bundle's
``connections.oauth.public_base_url`` — the exact key OAuth redirect
building already uses (``integrations/connections/oauth.py::callback_url``).
One value per deployment process, seeded by the hub's ``on_bundle_load`` so
that every surface has it, including those that never construct a delegated
client; a reload re-reads a changed descriptor. Delegated client construction
also seeds it, which keeps a hub loaded by an older host working.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)

PUBLIC_BASE_URL_CONFIG_KEY = "connections.oauth.public_base_url"

_public_base_url: str = ""
_warned_relative: bool = False


def set_connection_hub_public_base_url(value: Any) -> None:
    """Remember the deployment's public base URL (empty clears it)."""
    global _public_base_url
    _public_base_url = str(value or "").strip().rstrip("/")


def connection_hub_public_base_url() -> str:
    return _public_base_url


def public_base_url_from_hub_props(props: Mapping[str, Any] | None) -> str:
    """Extract ``connections.oauth.public_base_url`` from hub bundle props."""
    if not isinstance(props, Mapping):
        return ""
    connections = props.get("connections")
    if not isinstance(connections, Mapping):
        return ""
    oauth = connections.get("oauth")
    if not isinstance(oauth, Mapping):
        return ""
    return str(oauth.get("public_base_url") or "").strip().rstrip("/")


def connection_hub_public_url(path: str) -> str:
    """Prefix a hub path with the deployment's public base.

    The link travels beyond the app origin, so it ships absolute whenever the
    base is known. With no base the path is still openable inside the app
    origin and is returned as-is; one warning per process names the key that
    would make it absolute. An empty destination is never returned for a path
    that exists, because a caller cannot tell it apart from "no recovery".
    """
    clean = str(path or "")
    if not clean:
        return ""
    base = connection_hub_public_base_url()
    if base:
        return f"{base}{clean}"
    global _warned_relative
    if not _warned_relative:
        _warned_relative = True
        LOGGER.warning(
            "[connection-hub.public-base] hub deep link stays RELATIVE: set %s "
            "in the Connection Hub bundle config so external clients receive "
            "an absolute URL",
            PUBLIC_BASE_URL_CONFIG_KEY,
        )
    return clean


def is_openable_hub_url(value: Any) -> bool:
    """Whether a link can be opened by someone outside the app origin.

    An external MCP client relays the link verbatim to a user whose browser has
    no notion of the deployment host, so only an absolute URL reaches them. A
    relative fallback still works inside the app origin.
    """
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


__all__ = [
    "PUBLIC_BASE_URL_CONFIG_KEY",
    "connection_hub_public_base_url",
    "connection_hub_public_url",
    "is_openable_hub_url",
    "public_base_url_from_hub_props",
    "set_connection_hub_public_base_url",
]

"""URL checks for the authorization callback.

Adapted from KDCube's ``kdcube_cli.management.validation.validate_web_url``
(MIT, same copyright holder) so the caller layer needs no KDCube package (W322).
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from connection_hub.caller.errors import AuthorizationError


def _is_loopback(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def validate_web_url(
    value: Any,
    *,
    code: str,
    allow_query: bool = True,
    loopback_only: bool = False,
) -> str:
    """Validate an HTTPS URL, permitting HTTP only for loopback targets."""

    raw = str(value or "").strip()
    try:
        if not raw or len(raw) > 8192:
            raise ValueError(raw)
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        raise AuthorizationError(code, "The authorization server published an invalid URL.") from None
    hostname = parsed.hostname or ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_query and parsed.query)
    ):
        raise AuthorizationError(code, "The authorization server published an invalid URL.")
    if parsed.scheme.lower() == "http" and not _is_loopback(hostname):
        raise AuthorizationError(code, "Authorization requires HTTPS except on this device.")
    if loopback_only and not _is_loopback(hostname):
        raise AuthorizationError(code, "The authorization callback must resolve to this device.")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query if allow_query else "",
            "",
        )
    )


__all__ = ["validate_web_url"]

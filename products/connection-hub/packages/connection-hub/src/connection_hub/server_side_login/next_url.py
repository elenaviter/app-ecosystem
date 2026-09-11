# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The post-login destination guard.

A login starts with ``next=<where to return>`` and the callback redirects
there. Anything but a same-origin absolute path turns the sign-in into an
open redirect, so the guard accepts only a path: leading slash, no scheme, no
host, no protocol-relative ``//``, no backslash (browsers read ``/\\host`` as
a host), no control characters, and it keeps the query and fragment.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

DEFAULT_NEXT_PATH = "/"


def safe_next_path(raw: str | None, *, default: str = DEFAULT_NEXT_PATH) -> str:
    """``raw`` when it is a same-origin absolute path, else ``default``."""
    text = str(raw or "").strip()
    if not text or not text.startswith("/"):
        return default
    if text.startswith("//") or text.startswith("/\\"):
        return default
    if "\\" in text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return default
    parts = urlsplit(text)
    if parts.scheme or parts.netloc:
        return default
    if not parts.path.startswith("/"):
        return default
    return text


def _origin_allowed(origin: str, allowed_origins: Iterable[str]) -> bool:
    """Exact origin match, or a ``https://*.example.com`` wildcard that
    matches any single-label subdomain (never the bare domain)."""
    for pattern in allowed_origins:
        candidate = str(pattern or "").strip().lower().rstrip("/")
        if not candidate:
            continue
        if candidate == origin:
            return True
        scheme, sep, host = candidate.partition("://")
        if sep and host.startswith("*."):
            origin_scheme, _, origin_host = origin.partition("://")
            suffix = host[1:]  # ".example.com"
            label = origin_host[: -len(suffix)] if origin_host.endswith(suffix) else ""
            if origin_scheme == scheme and label and "." not in label and "/" not in label:
                return True
    return False


def safe_next_target(raw: str | None, *, allowed_origins: Iterable[str] = (), default: str = DEFAULT_NEXT_PATH) -> str:
    """``raw`` when it is a same-origin absolute path, or an absolute
    ``https``/``http`` URL whose origin is in ``allowed_origins``; else
    ``default``. A site on another origin of the deployment (a website beside
    the platform) can only be returned to when the deployment lists it."""
    text = str(raw or "").strip()
    if not text:
        return default
    if text.startswith("/"):
        return safe_next_path(text, default=default)
    if "\\" in text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return default
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return default
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if not _origin_allowed(origin, allowed_origins):
        return default
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return default
    rebuilt = f"{origin}{path}"
    if parts.query:
        rebuilt += f"?{parts.query}"
    if parts.fragment:
        rebuilt += f"#{parts.fragment}"
    return rebuilt


__all__ = ["DEFAULT_NEXT_PATH", "safe_next_path", "safe_next_target"]

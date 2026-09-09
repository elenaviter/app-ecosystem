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


__all__ = ["DEFAULT_NEXT_PATH", "safe_next_path"]

from __future__ import annotations

import hashlib


def bearer_sha256(value: str) -> str:
    """Return the non-recoverable identity of a high-entropy bearer."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = ["bearer_sha256"]

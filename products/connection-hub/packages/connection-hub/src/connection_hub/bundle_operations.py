# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Portable response helpers for request-bound bundle operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_bundle_operation_result(
    operation: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the operation payload from a supported transport shape.

    Protocol owners validate the returned payload. An unknown mapping remains
    unchanged so their required fields continue to provide the default-closed
    boundary.
    """

    result = dict(value or {})
    if "ok" in result:
        return result
    nested = result.get(str(operation or "").strip())
    if isinstance(nested, Mapping):
        return dict(nested)
    if str(result.get("status") or "").strip().lower() == "ok":
        nested = result.get("result")
        if isinstance(nested, Mapping):
            return dict(nested)
    return result


__all__ = ["normalize_bundle_operation_result"]

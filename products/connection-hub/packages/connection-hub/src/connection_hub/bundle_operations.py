# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Portable response helpers for request-bound bundle operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BUNDLE_OPERATION_RESULT_SHAPE_INVALID = "bundle_operation_result_shape_invalid"


class BundleOperationResultError(ValueError):
    """A bundle-operation answer has no supported result envelope."""

    def __init__(self, reason: str = BUNDLE_OPERATION_RESULT_SHAPE_INVALID) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_bundle_operation_result(
    operation: str,
    value: Any,
) -> dict[str, Any]:
    """Return the operation payload from a supported transport shape.

    Protocol owners validate the returned payload. Transport shapes outside
    this contract raise a stable, named error.
    """

    if not isinstance(value, Mapping):
        raise BundleOperationResultError()
    result = dict(value)
    if "ok" in result:
        return result
    operation = str(operation or "").strip()
    status_present = "status" in result
    status = str(result.get("status") or "").strip().lower()
    nested = result.get(operation)
    if isinstance(nested, Mapping) and (not status_present or status == "ok"):
        return dict(nested)
    if status == "ok":
        nested = result.get("result")
        if isinstance(nested, Mapping):
            return dict(nested)
    raise BundleOperationResultError()


__all__ = [
    "BUNDLE_OPERATION_RESULT_SHAPE_INVALID",
    "BundleOperationResultError",
    "normalize_bundle_operation_result",
]

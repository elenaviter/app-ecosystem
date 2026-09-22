# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Transaction-scoped locks for resident-secret references."""

from __future__ import annotations

from typing import Any


async def lock_resident_secret_refs(connection: Any, *secret_refs: str) -> None:
    """Serialize cross-table ownership changes for each canonical reference."""

    references = sorted(
        {
            str(value or "").strip()
            for value in secret_refs
            if str(value or "").strip()
        }
    )
    for reference in references:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))",
            reference,
            0,
        )


__all__ = ["lock_resident_secret_refs"]

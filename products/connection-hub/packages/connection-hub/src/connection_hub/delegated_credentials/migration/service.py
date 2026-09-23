# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Storage-neutral orchestration for migration preview generation."""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.delegated_credentials.migration.apply import (
    AuthorityMigrationSource,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationPreview,
    build_migration_preview,
)


async def create_migration_preview(
    *,
    source: AuthorityMigrationSource,
    generation_id: str,
    prerequisites: Mapping[str, Any],
    captured_at_ms: int | None = None,
) -> AuthorityMigrationPreview:
    """Inspect once and produce the complete secret-safe review artifact."""

    inspection = await source.inspect(captured_at_ms=captured_at_ms)
    return build_migration_preview(
        inspection,
        generation_id=generation_id,
        prerequisites=prerequisites,
    )


__all__ = ["create_migration_preview"]

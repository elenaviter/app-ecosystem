# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Asynchronous filesystem boundary for reviewed migration previews."""

from __future__ import annotations

import pathlib
from collections.abc import Mapping

from connection_hub.delegated_credentials.durable_io import (
    DurableDecodeError,
    read_json_or_none,
    write_json_atomic,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationPreview,
)


class MigrationPreviewArtifactError(RuntimeError):
    """A requested preview artifact is absent or structurally invalid."""


async def write_migration_preview(
    path: str | pathlib.Path,
    preview: AuthorityMigrationPreview,
) -> pathlib.Path:
    target = pathlib.Path(path).expanduser()
    await write_json_atomic(target, preview.validated().to_dict())
    return target


async def read_migration_preview(
    path: str | pathlib.Path,
) -> AuthorityMigrationPreview:
    target = pathlib.Path(path).expanduser()
    try:
        payload = await read_json_or_none(target)
    except DurableDecodeError as exc:
        raise MigrationPreviewArtifactError(
            "authority_migration_preview_not_json"
        ) from exc
    if payload is None:
        raise MigrationPreviewArtifactError("authority_migration_preview_missing")
    if not isinstance(payload, Mapping):
        raise MigrationPreviewArtifactError("authority_migration_preview_not_an_object")
    try:
        return AuthorityMigrationPreview.from_mapping(payload)
    except (TypeError, ValueError) as exc:
        raise MigrationPreviewArtifactError(
            "authority_migration_preview_invalid"
        ) from exc


__all__ = [
    "MigrationPreviewArtifactError",
    "read_migration_preview",
    "write_migration_preview",
]

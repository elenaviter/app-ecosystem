# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Typed PostgreSQL row mappings for delegated-Card handle authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    PreparedResidentSecret,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)


def card_handle_columns(alias: str = "") -> str:
    prefix = f"{str(alias).strip()}." if str(alias).strip() else ""
    return f"""
    {prefix}access_id,
    {prefix}card_revision,
    {prefix}resident_access_secret_ref,
    {prefix}resident_access_sha256,
    {prefix}session_id,
    {prefix}state,
    {prefix}revision,
    EXTRACT(EPOCH FROM {prefix}created_at)::bigint AS created_at,
    EXTRACT(EPOCH FROM {prefix}updated_at)::bigint AS updated_at,
    COALESCE(EXTRACT(EPOCH FROM {prefix}retired_at)::bigint, 0) AS retired_at,
    EXTRACT(EPOCH FROM {prefix}expires_at)::bigint AS expires_at
"""


CARD_HANDLE_COLUMNS = card_handle_columns()

PREPARED_SECRET_COLUMNS = """
    access_id,
    secret_ref,
    resident_access_sha256,
    card_revision,
    EXTRACT(EPOCH FROM envelope_created_at)::bigint AS created_at,
    EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at,
    state
"""

RETIRED_SECRET_COLUMNS = """
    access_id,
    secret_ref,
    resident_access_sha256,
    EXTRACT(EPOCH FROM retired_at)::bigint AS retired_at
"""

def card_handle_from_row(
    row: Mapping[str, Any] | None,
) -> CardHandleMetadata | None:
    if row is None:
        return None
    return CardHandleMetadata(
        access_id=str(row.get("access_id") or ""),
        card_revision=int(row.get("card_revision") or 0),
        expires_at=int(row.get("expires_at") or 0),
        resident_access_secret_ref=str(row.get("resident_access_secret_ref") or ""),
        resident_access_sha256=str(row.get("resident_access_sha256") or ""),
        session_id=str(row.get("session_id") or ""),
        state=str(row.get("state") or ""),
        revision=int(row.get("revision") or 0),
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
        retired_at=int(row.get("retired_at") or 0),
    ).validated()


def prepared_secret_from_row(
    row: Mapping[str, Any] | None,
) -> PreparedResidentSecret | None:
    if row is None:
        return None
    return PreparedResidentSecret(
        access_id=str(row.get("access_id") or ""),
        secret_ref=str(row.get("secret_ref") or ""),
        resident_access_sha256=str(row.get("resident_access_sha256") or ""),
        card_revision=int(row.get("card_revision") or 0),
        created_at=int(row.get("created_at") or 0),
        expires_at=int(row.get("expires_at") or 0),
        state=str(row.get("state") or ""),
    ).validated()


def retired_secret_from_row(
    row: Mapping[str, Any] | None,
) -> RetiredResidentSecret | None:
    if row is None:
        return None
    return RetiredResidentSecret(
        access_id=str(row.get("access_id") or ""),
        secret_ref=str(row.get("secret_ref") or ""),
        resident_access_sha256=str(row.get("resident_access_sha256") or ""),
        retired_at=int(row.get("retired_at") or 0),
    ).validated()


def cleanup_claim_from_row(
    row: Mapping[str, Any] | None,
) -> ResidentSecretCleanupClaim | None:
    if row is None:
        return None
    return ResidentSecretCleanupClaim(
        source=str(row.get("source") or ""),
        access_id=str(row.get("access_id") or ""),
        secret_ref=str(row.get("secret_ref") or ""),
        resident_access_sha256=str(row.get("resident_access_sha256") or ""),
        claim_token=str(row.get("claim_token") or ""),
        attempt=int(row.get("attempt") or 0),
        claimed_at=int(row.get("claimed_at") or 0),
        claim_expires_at=int(row.get("claim_expires_at") or 0),
        card_revision=int(row.get("card_revision") or 0),
        created_at=int(row.get("created_at") or 0),
        expires_at=int(row.get("expires_at") or 0),
        metadata_revision=int(row.get("metadata_revision") or 0),
    ).validated()


__all__ = [
    "CARD_HANDLE_COLUMNS",
    "PREPARED_SECRET_COLUMNS",
    "RETIRED_SECRET_COLUMNS",
    "card_handle_columns",
    "card_handle_from_row",
    "cleanup_claim_from_row",
    "prepared_secret_from_row",
    "retired_secret_from_row",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL schema for minimized delegated-Card credential metadata."""

from __future__ import annotations

from connection_hub.hub.authenticator_store import schema_for_scope


TABLE_CARD_HANDLE_METADATA = "connection_hub_card_handle_metadata"
TABLE_RETIRED_RESIDENT_SECRETS = "connection_hub_card_retired_resident_secrets"


def card_handle_schema(*, tenant: str, project: str) -> str:
    return schema_for_scope(tenant=tenant, project=project)


def card_handle_schema_sql(schema: str) -> str:
    """Return DDL for non-secret Card-handle metadata.

    OAuth refresh generations and access bindings already carry
    ``registry_access_id`` in their owning tables. This row therefore stores
    only the metadata that has no other durable owner: an optional host-vault
    reference for a resident-agent bearer and a durable session identifier.
    """

    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_CARD_HANDLE_METADATA} (
    access_id                       TEXT PRIMARY KEY,
    tenant                          TEXT NOT NULL,
    project                         TEXT NOT NULL,
    card_revision                   BIGINT NOT NULL CHECK (card_revision >= 1),
    resident_access_secret_ref      TEXT NOT NULL DEFAULT '',
    resident_access_sha256          TEXT NOT NULL DEFAULT '',
    session_id                      TEXT NOT NULL DEFAULT '',
    state                           TEXT NOT NULL DEFAULT 'active'
                                    CHECK (state IN ('active', 'revoked', 'expired')),
    revision                        BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at                      TIMESTAMPTZ,
    expires_at                      TIMESTAMPTZ NOT NULL,
    CHECK (
        (resident_access_secret_ref = '' AND resident_access_sha256 = '')
        OR (
            resident_access_secret_ref <> ''
            AND length(resident_access_sha256) = 64
            AND resident_access_sha256 ~ '^[0-9a-f]{{64}}$'
        )
    ),
    CHECK (
        (state = 'active' AND retired_at IS NULL)
        OR (state <> 'active' AND retired_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_expiry_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (expires_at)
    WHERE state = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_card_handle_current_secret_ref_uidx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (resident_access_secret_ref)
    WHERE resident_access_secret_ref <> '';

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_secret_cleanup_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (retired_at, access_id)
    WHERE state <> 'active' AND resident_access_secret_ref <> '';

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_retention_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (retired_at, access_id)
    WHERE state <> 'active' AND resident_access_secret_ref = '';

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_RETIRED_RESIDENT_SECRETS} (
    secret_ref                      TEXT PRIMARY KEY,
    access_id                       TEXT NOT NULL
                                    REFERENCES
                                    {schema}.{TABLE_CARD_HANDLE_METADATA}(access_id)
                                    ON DELETE RESTRICT,
    resident_access_sha256          TEXT NOT NULL
                                    CHECK (resident_access_sha256 ~ '^[0-9a-f]{{64}}$'),
    retired_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS connection_hub_card_retired_secret_cleanup_idx
    ON {schema}.{TABLE_RETIRED_RESIDENT_SECRETS} (retired_at, access_id, secret_ref);
"""


__all__ = [
    "TABLE_CARD_HANDLE_METADATA",
    "TABLE_RETIRED_RESIDENT_SECRETS",
    "card_handle_schema",
    "card_handle_schema_sql",
]

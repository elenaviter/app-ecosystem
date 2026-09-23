from __future__ import annotations

from connection_hub.delegated_credentials.authority_cutover import (
    TABLE_AUTHORITY_CUTOVERS,
    authority_cutover_schema_sql,
)
from connection_hub.hub.authenticator_store import schema_for_scope

TABLE_CLIENTS = "connection_hub_oauth_clients"
TABLE_FAMILIES = "connection_hub_oauth_credential_families"
TABLE_REFRESH_GENERATIONS = "connection_hub_oauth_refresh_generations"
TABLE_ACCESS_BINDINGS = "connection_hub_oauth_access_bindings"
# Compatibility alias for callers that imported the original OAuth-local name.
TABLE_CUTOVERS = TABLE_AUTHORITY_CUTOVERS


def oauth_authority_schema(*, tenant: str, project: str) -> str:
    return schema_for_scope(tenant=tenant, project=project)


def oauth_authority_schema_sql(schema: str) -> str:
    """DDL for durable OAuth authority.

    The caller supplies a schema produced by :func:`oauth_authority_schema`,
    whose identifier sanitizer makes interpolation safe. Bearer values are
    represented only by SHA-256 hashes.
    """

    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_CLIENTS} (
    client_id                    TEXT PRIMARY KEY,
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    redirect_uris                JSONB NOT NULL,
    grant_types                  JSONB NOT NULL
                                 DEFAULT '["authorization_code","refresh_token"]'::jsonb,
    token_endpoint_auth_method   TEXT NOT NULL DEFAULT 'none',
    application_type             TEXT NOT NULL DEFAULT 'native',
    metadata                     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                   TIMESTAMPTZ NOT NULL,
    retired_at                   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_clients_live_idx
    ON {schema}.{TABLE_CLIENTS} (expires_at)
    WHERE retired_at IS NULL;

ALTER TABLE {schema}.{TABLE_CLIENTS}
    ADD COLUMN IF NOT EXISTS grant_types JSONB NOT NULL
    DEFAULT '["authorization_code","refresh_token"]'::jsonb;

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_FAMILIES} (
    family_id                    TEXT PRIMARY KEY,
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    registry_access_id           TEXT NOT NULL DEFAULT '',
    card_kind                    TEXT NOT NULL DEFAULT '',
    client_id                    TEXT NOT NULL,
    subject                      TEXT NOT NULL,
    identity_scope               TEXT NOT NULL DEFAULT '',
    current_generation_id        TEXT NOT NULL,
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'revoked', 'expired')),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_families_card_idx
    ON {schema}.{TABLE_FAMILIES} (registry_access_id, state);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_families_client_idx
    ON {schema}.{TABLE_FAMILIES} (client_id, subject, state);

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_REFRESH_GENERATIONS} (
    generation_id                TEXT PRIMARY KEY,
    family_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE CASCADE,
    token_sha256                 CHAR(64) NOT NULL UNIQUE,
    record                       JSONB NOT NULL,
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'consumed', 'revoked', 'expired')),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at                  TIMESTAMPTZ,
    revoked_at                   TIMESTAMPTZ,
    expires_at                   TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_refresh_one_active_idx
    ON {schema}.{TABLE_REFRESH_GENERATIONS} (family_id)
    WHERE state = 'active';

CREATE INDEX IF NOT EXISTS connection_hub_oauth_refresh_expiry_idx
    ON {schema}.{TABLE_REFRESH_GENERATIONS} (expires_at)
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_ACCESS_BINDINGS} (
    token_sha256                 CHAR(64) PRIMARY KEY,
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    registry_access_id           TEXT NOT NULL DEFAULT '',
    record                       JSONB NOT NULL,
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'revoked', 'expired')),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                   TIMESTAMPTZ,
    expires_at                   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_access_card_idx
    ON {schema}.{TABLE_ACCESS_BINDINGS} (registry_access_id, state);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_access_expiry_idx
    ON {schema}.{TABLE_ACCESS_BINDINGS} (expires_at)
    WHERE state = 'active';

""" + authority_cutover_schema_sql(schema)

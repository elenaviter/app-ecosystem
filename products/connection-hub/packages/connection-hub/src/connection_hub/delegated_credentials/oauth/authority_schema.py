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
# W603: an original OAuth issuance's credentials before its Card decision's COMMIT.
TABLE_ISSUANCE_RESERVATIONS = "connection_hub_oauth_issuance_reservations"
TABLE_ISSUANCE_PLANS = "connection_hub_oauth_issuance_plans"
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

-- W585: the Card's committed absolute deadline and the Card revision that set
-- it. Rotation never extends a family past this cap (LEAST in the successor
-- statement), and a lifetime effect from an older Card revision never moves it.
ALTER TABLE {schema}.{TABLE_FAMILIES}
    ADD COLUMN IF NOT EXISTS cap_expires_at TIMESTAMPTZ NULL;
ALTER TABLE {schema}.{TABLE_FAMILIES}
    ADD COLUMN IF NOT EXISTS card_revision BIGINT NOT NULL DEFAULT 0;

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

-- W585 (Ops W1): the Card revision whose lifetime last set this binding's
-- deadline, so a replayed effect from an older revision never moves it.
ALTER TABLE {schema}.{TABLE_ACCESS_BINDINGS}
    ADD COLUMN IF NOT EXISTS card_revision BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS connection_hub_oauth_access_expiry_idx
    ON {schema}.{TABLE_ACCESS_BINDINGS} (expires_at)
    WHERE state = 'active';

-- W603: the immutable plan of one original OAuth issuance, written once
-- before its Card decision begins (first writer wins) so that every replay
-- rebuilds the identical decision draft and returns the identical plan,
-- deadlines included. ``transaction_id`` is the decision's, bound after begin.
CREATE TABLE IF NOT EXISTS {schema}.{TABLE_ISSUANCE_PLANS} (
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    decision_request_id          CHAR(64) NOT NULL,
    original_input_digest        CHAR(64) NOT NULL,
    transaction_id               TEXT NULL,
    plan                         JSONB NOT NULL,
    reserved_until               TIMESTAMPTZ NOT NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, project, decision_request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_issuance_plans_transaction_idx
    ON {schema}.{TABLE_ISSUANCE_PLANS} (tenant, project, transaction_id)
    WHERE transaction_id IS NOT NULL;

-- W603: the credentials of an original issuance, reserved under its ONE Card
-- decision before that decision's COMMIT. A reservation is in no table a
-- reader looks at: activation inserts the real family/generation or access
-- binding from it after COMMIT, under the Card's family lock, and pins the
-- outcome. One row per (transaction, slot); the bearer is stored only as its
-- digest. ``reserved_until`` is fixed from the database clock at the plan and
-- never renewed; ``bound_*`` is written at the decision's STAGE.
CREATE TABLE IF NOT EXISTS {schema}.{TABLE_ISSUANCE_RESERVATIONS} (
    reservation_id               TEXT PRIMARY KEY,
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    transaction_id               TEXT NOT NULL,
    slot                         TEXT NOT NULL CHECK (slot IN ('access', 'refresh')),
    token_sha256                 CHAR(64) NOT NULL UNIQUE,
    record                       JSONB NOT NULL,
    registry_access_id           TEXT NOT NULL,
    subject                      TEXT NOT NULL,
    client_id                    TEXT NOT NULL,
    card_revision                BIGINT NOT NULL CHECK (card_revision >= 1),
    cap_expires_at               TIMESTAMPTZ NOT NULL,
    ttl_seconds                  INTEGER NOT NULL CHECK (ttl_seconds >= 1),
    original_input_digest        CHAR(64) NOT NULL,
    reservation_digest           CHAR(64) NOT NULL,
    reserved_until               TIMESTAMPTZ NOT NULL,
    state                        TEXT NOT NULL DEFAULT 'reserved'
                                 CHECK (state IN ('reserved', 'bound', 'activated', 'released', 'expired')),
    outcome                      TEXT NOT NULL DEFAULT ''
                                 CHECK (outcome IN ('', 'applied', 'superseded', 'released', 'expired')),
    pin                          TEXT NOT NULL DEFAULT '',
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant, project, transaction_id, slot)
);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_issuance_reservations_open_idx
    ON {schema}.{TABLE_ISSUANCE_RESERVATIONS} (reserved_until)
    WHERE state = 'reserved';

""" + authority_cutover_schema_sql(schema)

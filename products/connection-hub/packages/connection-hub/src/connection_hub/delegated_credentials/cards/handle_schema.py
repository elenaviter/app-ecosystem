# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL schema for minimized delegated-Card credential metadata."""

from __future__ import annotations

from connection_hub.hub.authenticator_store import schema_for_scope

TABLE_CARD_HANDLE_METADATA = "connection_hub_card_handle_metadata"
TABLE_PREPARED_RESIDENT_SECRETS = "connection_hub_card_prepared_resident_secrets"
TABLE_RETIRED_RESIDENT_SECRETS = "connection_hub_card_retired_resident_secrets"


def _validated_check_constraint_sql(
    *,
    schema: str,
    table: str,
    name: str,
    expression: str,
) -> str:
    add_statement = (
        f"ALTER TABLE {schema}.{table} ADD CONSTRAINT {name} "
        f"CHECK ({expression}) NOT VALID"
    ).replace("'", "''")
    return f"""
DO $constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = '{name}'
          AND conrelid = '{schema}.{table}'::regclass
    ) THEN
        EXECUTE '{add_statement}';
    END IF;
END
$constraint$;
ALTER TABLE {schema}.{table} VALIDATE CONSTRAINT {name};
"""


def card_handle_schema(*, tenant: str, project: str) -> str:
    return schema_for_scope(tenant=tenant, project=project)


def card_handle_schema_sql(schema: str) -> str:
    """Return DDL for non-secret Card-handle metadata.

    OAuth refresh generations and access bindings already carry
    ``registry_access_id`` in their owning tables. This row therefore stores
    only the metadata that has no other durable owner: an optional host-vault
    reference for a resident-agent bearer and a durable session identifier.
    """

    cleanup_claim_check = """
        (cleanup_claim_token = ''
            AND cleanup_claimed_at IS NULL
            AND cleanup_claim_expires_at IS NULL)
        OR
        (cleanup_claim_token ~ '^[0-9a-f]{32}$'
            AND cleanup_claimed_at IS NOT NULL
            AND cleanup_claim_expires_at IS NOT NULL
            AND cleanup_claim_expires_at > cleanup_claimed_at)
    """
    metadata_constraints = "\n".join(
        (
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_CARD_HANDLE_METADATA,
                name="ch_card_handle_cleanup_attempts_ck",
                expression="cleanup_attempts >= 0",
            ),
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_CARD_HANDLE_METADATA,
                name="ch_card_handle_cleanup_claim_ck",
                expression=cleanup_claim_check,
            ),
        )
    )
    prepared_constraints = "\n".join(
        (
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_PREPARED_RESIDENT_SECRETS,
                name="ch_card_prepared_state_ck",
                expression="state IN ('installable', 'cleanup')",
            ),
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_PREPARED_RESIDENT_SECRETS,
                name="ch_card_prepared_cleanup_attempts_ck",
                expression="cleanup_attempts >= 0",
            ),
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_PREPARED_RESIDENT_SECRETS,
                name="ch_card_prepared_cleanup_claim_ck",
                expression=cleanup_claim_check,
            ),
        )
    )
    retired_constraints = "\n".join(
        (
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_RETIRED_RESIDENT_SECRETS,
                name="ch_card_retired_cleanup_attempts_ck",
                expression="cleanup_attempts >= 0",
            ),
            _validated_check_constraint_sql(
                schema=schema,
                table=TABLE_RETIRED_RESIDENT_SECRETS,
                name="ch_card_retired_cleanup_claim_ck",
                expression=cleanup_claim_check,
            ),
        )
    )

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
    cleanup_attempts                BIGINT NOT NULL DEFAULT 0
                                    CHECK (cleanup_attempts >= 0),
    cleanup_claim_token             TEXT NOT NULL DEFAULT '',
    cleanup_claimed_at              TIMESTAMPTZ,
    cleanup_claim_expires_at        TIMESTAMPTZ,
    cleanup_next_attempt_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleanup_last_error              TEXT NOT NULL DEFAULT '',
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

ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_attempts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_claim_token TEXT NOT NULL DEFAULT '';
ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_claimed_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_claim_expires_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE {schema}.{TABLE_CARD_HANDLE_METADATA}
    ADD COLUMN IF NOT EXISTS cleanup_last_error TEXT NOT NULL DEFAULT '';

{metadata_constraints}

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_expiry_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (expires_at)
    WHERE state = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_card_handle_current_secret_ref_uidx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (resident_access_secret_ref)
    WHERE resident_access_secret_ref <> '';

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_secret_cleanup_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (retired_at, access_id)
    WHERE state <> 'active' AND resident_access_secret_ref <> '';

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_cleanup_due_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA}
       (cleanup_next_attempt_at, retired_at, access_id)
    WHERE state <> 'active' AND resident_access_secret_ref <> '';

CREATE INDEX IF NOT EXISTS connection_hub_card_handle_retention_idx
    ON {schema}.{TABLE_CARD_HANDLE_METADATA} (retired_at, access_id)
    WHERE state <> 'active' AND resident_access_secret_ref = '';

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_PREPARED_RESIDENT_SECRETS} (
    secret_ref                      TEXT PRIMARY KEY,
    access_id                       TEXT NOT NULL,
    card_revision                   BIGINT NOT NULL CHECK (card_revision >= 1),
    resident_access_sha256          TEXT NOT NULL
                                    CHECK (resident_access_sha256 ~ '^[0-9a-f]{{64}}$'),
    envelope_created_at             TIMESTAMPTZ NOT NULL,
    expires_at                      TIMESTAMPTZ NOT NULL,
    prepared_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    state                            TEXT NOT NULL DEFAULT 'installable'
                                    CHECK (state IN ('installable', 'cleanup')),
    cleanup_attempts                 BIGINT NOT NULL DEFAULT 0
                                    CHECK (cleanup_attempts >= 0),
    cleanup_claim_token              TEXT NOT NULL DEFAULT '',
    cleanup_claimed_at               TIMESTAMPTZ,
    cleanup_claim_expires_at         TIMESTAMPTZ,
    cleanup_next_attempt_at          TIMESTAMPTZ NOT NULL
                                    DEFAULT now() + INTERVAL '5 minutes',
    cleanup_last_error               TEXT NOT NULL DEFAULT '',
    CHECK (expires_at > envelope_created_at)
);

ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'installable';
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_attempts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claim_token TEXT NOT NULL DEFAULT '';
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claimed_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claim_expires_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_next_attempt_at TIMESTAMPTZ NOT NULL
    DEFAULT now() + INTERVAL '5 minutes';
ALTER TABLE {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_last_error TEXT NOT NULL DEFAULT '';

{prepared_constraints}

CREATE INDEX IF NOT EXISTS connection_hub_card_prepared_secret_cleanup_idx
    ON {schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
       (cleanup_next_attempt_at, prepared_at, access_id, secret_ref);

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_RETIRED_RESIDENT_SECRETS} (
    secret_ref                      TEXT PRIMARY KEY,
    access_id                       TEXT NOT NULL
                                    REFERENCES
                                    {schema}.{TABLE_CARD_HANDLE_METADATA}(access_id)
                                    ON DELETE RESTRICT,
    resident_access_sha256          TEXT NOT NULL
                                    CHECK (resident_access_sha256 ~ '^[0-9a-f]{{64}}$'),
    retired_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleanup_attempts                BIGINT NOT NULL DEFAULT 0
                                    CHECK (cleanup_attempts >= 0),
    cleanup_claim_token             TEXT NOT NULL DEFAULT '',
    cleanup_claimed_at              TIMESTAMPTZ,
    cleanup_claim_expires_at        TIMESTAMPTZ,
    cleanup_next_attempt_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleanup_last_error              TEXT NOT NULL DEFAULT ''
);

ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_attempts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claim_token TEXT NOT NULL DEFAULT '';
ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claimed_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_claim_expires_at TIMESTAMPTZ;
ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
    ADD COLUMN IF NOT EXISTS cleanup_last_error TEXT NOT NULL DEFAULT '';

{retired_constraints}

CREATE INDEX IF NOT EXISTS connection_hub_card_retired_secret_cleanup_idx
    ON {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
       (retired_at, access_id, secret_ref);

CREATE INDEX IF NOT EXISTS connection_hub_card_retired_secret_cleanup_due_idx
    ON {schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
       (cleanup_next_attempt_at, retired_at, access_id, secret_ref);
"""


__all__ = [
    "TABLE_CARD_HANDLE_METADATA",
    "TABLE_PREPARED_RESIDENT_SECRETS",
    "TABLE_RETIRED_RESIDENT_SECRETS",
    "card_handle_schema",
    "card_handle_schema_sql",
]

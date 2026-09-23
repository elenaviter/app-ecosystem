from __future__ import annotations

from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_FAMILIES,
)

TABLE_PROFILE_DEVICES = "connection_hub_oauth_profile_devices"
TABLE_DEVICE_FAMILIES = "connection_hub_oauth_device_families"
TABLE_DEVICE_ACCESS_BINDINGS = "connection_hub_oauth_device_access_bindings"
TABLE_RECOVERY_ATTEMPTS = "connection_hub_oauth_device_recovery_attempts"
TABLE_DEVICE_PACKAGES = "connection_hub_oauth_device_packages"


def profile_device_authority_schema_sql(schema: str) -> str:
    """Additive DDL for profile devices and encrypted credential delivery."""

    return f"""
CREATE TABLE IF NOT EXISTS {schema}.{TABLE_PROFILE_DEVICES} (
    device_id                    TEXT PRIMARY KEY,
    tenant                       TEXT NOT NULL,
    project                      TEXT NOT NULL,
    registry_access_id           TEXT NOT NULL,
    card_kind                    TEXT NOT NULL,
    enrollment_family_id         TEXT NOT NULL UNIQUE
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE RESTRICT,
    current_family_id            TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE RESTRICT,
    client_id                    TEXT NOT NULL,
    subject                      TEXT NOT NULL,
    identity_scope               TEXT NOT NULL DEFAULT '',
    device_label                 TEXT NOT NULL,
    public_jwk                   JSONB NOT NULL,
    thumbprint                   TEXT NOT NULL
                                 CHECK (length(thumbprint) = 43),
    enrolled_card_revision       BIGINT NOT NULL
                                 CHECK (enrolled_card_revision >= 1),
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'revoked')),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                   TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_profile_devices_key_idx
    ON {schema}.{TABLE_PROFILE_DEVICES} (thumbprint)
    WHERE state = 'active';

CREATE INDEX IF NOT EXISTS connection_hub_oauth_profile_devices_card_idx
    ON {schema}.{TABLE_PROFILE_DEVICES} (registry_access_id, state);

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_DEVICE_FAMILIES} (
    family_id                    TEXT PRIMARY KEY
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE RESTRICT,
    device_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_PROFILE_DEVICES}(device_id)
                                 ON DELETE RESTRICT,
    device_revision              BIGINT NOT NULL
                                 CHECK (device_revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'current'
                                 CHECK (state IN ('current', 'retired', 'revoked')),
    bound_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at                   TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_device_family_current_idx
    ON {schema}.{TABLE_DEVICE_FAMILIES} (device_id)
    WHERE state = 'current';

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_DEVICE_ACCESS_BINDINGS} (
    token_sha256                 CHAR(64) PRIMARY KEY
                                 REFERENCES {schema}.{TABLE_ACCESS_BINDINGS}(token_sha256)
                                 ON DELETE CASCADE,
    family_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE RESTRICT,
    device_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_PROFILE_DEVICES}(device_id)
                                 ON DELETE RESTRICT,
    device_revision              BIGINT NOT NULL
                                 CHECK (device_revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'active'
                                 CHECK (state IN ('active', 'revoked', 'expired')),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS connection_hub_oauth_device_access_family_idx
    ON {schema}.{TABLE_DEVICE_ACCESS_BINDINGS} (family_id, state);

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_RECOVERY_ATTEMPTS} (
    attempt_id                   TEXT PRIMARY KEY,
    device_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_PROFILE_DEVICES}(device_id)
                                 ON DELETE RESTRICT,
    device_revision              BIGINT NOT NULL
                                 CHECK (device_revision >= 1),
    registry_access_id           TEXT NOT NULL,
    card_revision                BIGINT NOT NULL
                                 CHECK (card_revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'waiting'
                                 CHECK (state IN (
                                     'waiting', 'ready', 'delivered', 'expired',
                                     'revoked', 'superseded'
                                 )),
    package_id                   TEXT,
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                   TIMESTAMPTZ NOT NULL,
    terminal_reason              TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_device_recovery_live_idx
    ON {schema}.{TABLE_RECOVERY_ATTEMPTS} (device_id)
    WHERE state IN ('waiting', 'ready');

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_DEVICE_PACKAGES} (
    package_id                   TEXT PRIMARY KEY,
    package_kind                 TEXT NOT NULL
                                 CHECK (package_kind IN ('enrollment', 'reissue')),
    attempt_id                   TEXT UNIQUE
                                 REFERENCES {schema}.{TABLE_RECOVERY_ATTEMPTS}(attempt_id)
                                 ON DELETE RESTRICT,
    device_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_PROFILE_DEVICES}(device_id)
                                 ON DELETE RESTRICT,
    device_revision              BIGINT NOT NULL
                                 CHECK (device_revision >= 1),
    family_id                    TEXT NOT NULL
                                 REFERENCES {schema}.{TABLE_FAMILIES}(family_id)
                                 ON DELETE RESTRICT,
    source_generation_id         TEXT,
    registry_access_id           TEXT NOT NULL,
    card_revision                BIGINT NOT NULL
                                 CHECK (card_revision >= 1),
    state                        TEXT NOT NULL DEFAULT 'ready'
                                 CHECK (state IN (
                                     'ready', 'delivered', 'expired', 'revoked',
                                     'superseded'
                                 )),
    jwe_ciphertext               TEXT,
    delivery_nonce_sha256        CHAR(64) NOT NULL,
    fetch_count                  BIGINT NOT NULL DEFAULT 0
                                 CHECK (fetch_count >= 0),
    revision                     BIGINT NOT NULL DEFAULT 1
                                 CHECK (revision >= 1),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                   TIMESTAMPTZ NOT NULL,
    first_fetched_at             TIMESTAMPTZ,
    last_fetched_at              TIMESTAMPTZ,
    delivered_at                 TIMESTAMPTZ,
    terminal_reason              TEXT NOT NULL DEFAULT '',
    CHECK (
        (state = 'ready' AND jwe_ciphertext IS NOT NULL)
        OR (state <> 'ready' AND jwe_ciphertext IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_oauth_device_enrollment_receipt_idx
    ON {schema}.{TABLE_DEVICE_PACKAGES} (source_generation_id)
    WHERE package_kind = 'enrollment';

CREATE INDEX IF NOT EXISTS connection_hub_oauth_device_package_ready_idx
    ON {schema}.{TABLE_DEVICE_PACKAGES} (device_id, expires_at)
    WHERE state = 'ready';
"""


__all__ = [
    "TABLE_DEVICE_ACCESS_BINDINGS",
    "TABLE_DEVICE_FAMILIES",
    "TABLE_DEVICE_PACKAGES",
    "TABLE_PROFILE_DEVICES",
    "TABLE_RECOVERY_ATTEMPTS",
    "profile_device_authority_schema_sql",
]

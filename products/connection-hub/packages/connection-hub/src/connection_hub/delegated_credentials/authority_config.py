# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Descriptor contract for the Connection Hub durable authority backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_ADMISSION_REPLAY,
    FAMILY_CARD_HANDLES,
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    FAMILY_RESIDENT_CARD_SECRETS,
)

AUTHORITY_BACKEND_POSTGRESQL = "postgresql"
AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE = "redis-migration-source"
AUTHORITY_BACKENDS = frozenset(
    {
        AUTHORITY_BACKEND_POSTGRESQL,
        AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE,
    }
)

CONNECTION_HUB_AUTHORITY_FAMILIES = (
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    FAMILY_OAUTH_ACCESS,
    FAMILY_CARD_HANDLES,
    FAMILY_RESIDENT_CARD_SECRETS,
    FAMILY_ADMISSION_REPLAY,
)


class DelegatedAuthorityConfigurationError(ValueError):
    """The descriptor does not select one complete authority generation."""


@dataclass(frozen=True)
class DelegatedAuthorityConfig:
    backend: str
    migration_id: str = ""

    @property
    def uses_postgresql(self) -> bool:
        return self.backend == AUTHORITY_BACKEND_POSTGRESQL

    @classmethod
    def from_connections(
        cls,
        connections: Mapping[str, Any],
    ) -> "DelegatedAuthorityConfig":
        delegated = connections.get("delegated_credentials")
        delegated_node = delegated if isinstance(delegated, Mapping) else {}
        raw = delegated_node.get("authority")
        if not isinstance(raw, Mapping):
            raise DelegatedAuthorityConfigurationError(
                "connections.delegated_credentials.authority is required"
            )
        backend = str(raw.get("backend") or "").strip().lower()
        if backend not in AUTHORITY_BACKENDS:
            raise DelegatedAuthorityConfigurationError(
                "connections.delegated_credentials.authority.backend is invalid"
            )
        migration_id = str(raw.get("migration_id") or "").strip()
        if backend == AUTHORITY_BACKEND_POSTGRESQL and not migration_id:
            raise DelegatedAuthorityConfigurationError(
                "PostgreSQL authority requires an activated migration_id"
            )
        if (
            backend == AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
            and migration_id
        ):
            raise DelegatedAuthorityConfigurationError(
                "Redis migration-source mode cannot claim an activated migration"
            )
        return cls(backend=backend, migration_id=migration_id)


__all__ = [
    "AUTHORITY_BACKENDS",
    "AUTHORITY_BACKEND_POSTGRESQL",
    "AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE",
    "CONNECTION_HUB_AUTHORITY_FAMILIES",
    "DelegatedAuthorityConfig",
    "DelegatedAuthorityConfigurationError",
]

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


class DurableAuthorityConfigurationError(ValueError):
    """The descriptor does not select one complete authority generation."""


@dataclass(frozen=True)
class DurableAuthorityConfig:
    backend: str
    generation_id: str = ""

    @property
    def uses_postgresql(self) -> bool:
        return self.backend == AUTHORITY_BACKEND_POSTGRESQL

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        field_path: str = "authority",
    ) -> "DurableAuthorityConfig":
        path = str(field_path or "authority").strip() or "authority"
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise DurableAuthorityConfigurationError(f"{path} is required")
        backend = (
            str(raw.get("backend") or "").strip().lower()
            or AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
        )
        if backend not in AUTHORITY_BACKENDS:
            raise DurableAuthorityConfigurationError(
                f"{path}.backend is invalid"
            )
        generation_id = str(raw.get("generation_id") or "").strip()
        if backend == AUTHORITY_BACKEND_POSTGRESQL and not generation_id:
            raise DurableAuthorityConfigurationError(
                "PostgreSQL authority requires an activated generation_id"
            )
        if (
            backend == AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
            and generation_id
        ):
            raise DurableAuthorityConfigurationError(
                "Redis migration-source mode cannot claim an activated generation"
            )
        return cls(backend=backend, generation_id=generation_id)

    @classmethod
    def from_connections(
        cls,
        connections: Mapping[str, Any],
    ) -> "DurableAuthorityConfig":
        delegated = connections.get("delegated_credentials")
        delegated_node = delegated if isinstance(delegated, Mapping) else {}
        return cls.from_mapping(
            delegated_node.get("authority"),
            field_path="connections.delegated_credentials.authority",
        )


# Compatibility names retain the app-facing vocabulary while the shared
# parser is also used by platform session authority configuration.
DelegatedAuthorityConfig = DurableAuthorityConfig
DelegatedAuthorityConfigurationError = DurableAuthorityConfigurationError


__all__ = [
    "AUTHORITY_BACKENDS",
    "AUTHORITY_BACKEND_POSTGRESQL",
    "AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE",
    "CONNECTION_HUB_AUTHORITY_FAMILIES",
    "DelegatedAuthorityConfig",
    "DelegatedAuthorityConfigurationError",
    "DurableAuthorityConfig",
    "DurableAuthorityConfigurationError",
]

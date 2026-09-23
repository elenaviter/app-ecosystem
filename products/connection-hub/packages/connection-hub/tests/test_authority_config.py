from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.authority_config import (
    AUTHORITY_BACKEND_POSTGRESQL,
    AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE,
    DelegatedAuthorityConfig,
    DelegatedAuthorityConfigurationError,
)


def _connections(authority: object) -> dict:
    return {"delegated_credentials": {"authority": authority}}


def test_postgresql_authority_names_the_activated_migration() -> None:
    config = DelegatedAuthorityConfig.from_connections(
        _connections(
            {
                "backend": "postgresql",
                "migration_id": "w253-durable-authority-v1",
            }
        )
    )

    assert config.backend == AUTHORITY_BACKEND_POSTGRESQL
    assert config.uses_postgresql is True
    assert config.migration_id == "w253-durable-authority-v1"


def test_redis_is_named_only_as_an_unactivated_migration_source() -> None:
    config = DelegatedAuthorityConfig.from_connections(
        _connections({"backend": "redis-migration-source"})
    )

    assert config.backend == AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
    assert config.uses_postgresql is False
    assert config.migration_id == ""


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {},
        {"backend": "redis"},
        {"backend": "postgresql"},
        {
            "backend": "redis-migration-source",
            "migration_id": "not-activated",
        },
    ],
)
def test_incomplete_or_ambiguous_authority_configuration_is_refused(
    authority: object,
) -> None:
    with pytest.raises(DelegatedAuthorityConfigurationError):
        DelegatedAuthorityConfig.from_connections(_connections(authority))

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.authority_config import (
    AUTHORITY_BACKEND_POSTGRESQL,
    AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE,
    DelegatedAuthorityConfig,
    DelegatedAuthorityConfigurationError,
    DurableAuthorityConfig,
)


def _connections(authority: object) -> dict:
    return {"delegated_credentials": {"authority": authority}}


def test_postgresql_authority_names_the_activated_migration() -> None:
    config = DelegatedAuthorityConfig.from_connections(
        _connections(
            {
                "backend": "postgresql",
                "generation_id": "durable-authority-v1",
            }
        )
    )

    assert config.backend == AUTHORITY_BACKEND_POSTGRESQL
    assert config.uses_postgresql is True
    assert config.generation_id == "durable-authority-v1"


def test_redis_is_named_only_as_an_unactivated_migration_source() -> None:
    config = DelegatedAuthorityConfig.from_connections(
        _connections({"backend": "redis-migration-source"})
    )

    assert config.backend == AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
    assert config.uses_postgresql is False
    assert config.generation_id == ""


@pytest.mark.parametrize(
    "connections",
    [
        {},
        {"delegated_credentials": {}},
        _connections(None),
        _connections({}),
        _connections({"backend": ""}),
    ],
)
def test_absent_backend_keeps_the_redis_migration_source(
    connections: dict,
) -> None:
    config = DelegatedAuthorityConfig.from_connections(connections)

    assert config.backend == AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE
    assert config.uses_postgresql is False
    assert config.generation_id == ""


def test_shared_parser_names_the_calling_descriptor_path() -> None:
    with pytest.raises(
        DelegatedAuthorityConfigurationError,
        match=r"auth\.sessions\.authority\.backend is invalid",
    ):
        DurableAuthorityConfig.from_mapping(
            {"backend": "redis"},
            field_path="auth.sessions.authority",
        )


@pytest.mark.parametrize(
    "authority",
    [
        "redis-migration-source",
        {"backend": "redis"},
        {"generation_id": "not-activated"},
        {"backend": "postgresql"},
        {
            "backend": "redis-migration-source",
            "generation_id": "not-activated",
        },
    ],
)
def test_incomplete_or_ambiguous_authority_configuration_is_refused(
    authority: object,
) -> None:
    with pytest.raises(DelegatedAuthorityConfigurationError):
        DelegatedAuthorityConfig.from_connections(_connections(authority))

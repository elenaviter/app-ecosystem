# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

import pytest

from connection_hub.delegated_credentials.cache_settings import (
    DEFAULT_ACTIVE_CACHE_SECONDS,
    DEFAULT_REVOKED_TOMBSTONE_SECONDS,
    DEFAULT_UPDATING_MARKER_SECONDS,
    DEFAULT_VERSION_CACHE_SECONDS,
    DelegatedCacheSettings,
)

def test_defaults_apply_when_the_block_is_absent():
    settings = DelegatedCacheSettings.from_connections({})
    assert settings.catalog.active_cache_seconds == DEFAULT_ACTIVE_CACHE_SECONDS
    assert settings.catalog.version_cache_seconds == DEFAULT_VERSION_CACHE_SECONDS
    assert settings.cards.updating_marker_seconds == DEFAULT_UPDATING_MARKER_SECONDS
    assert settings.cards.revoked_tombstone_seconds == DEFAULT_REVOKED_TOMBSTONE_SECONDS


def test_declared_values_are_read():
    settings = DelegatedCacheSettings.from_connections(
        {
            "delegated_credentials": {
                "catalog": {"active_cache_seconds": 60, "version_cache_seconds": 120},
                "cards": {"updating_marker_seconds": 5, "revoked_tombstone_seconds": 30},
            }
        }
    )
    assert settings.catalog.active_cache_seconds == 60
    assert settings.catalog.version_cache_seconds == 120
    assert settings.cards.updating_marker_seconds == 5
    assert settings.cards.revoked_tombstone_seconds == 30


@pytest.mark.parametrize("bad", [0, -1, "", None, "abc", {}])
def test_unusable_values_fall_back_to_the_default(bad):
    settings = DelegatedCacheSettings.from_connections(
        {"delegated_credentials": {"catalog": {"active_cache_seconds": bad}}}
    )
    assert settings.catalog.active_cache_seconds == DEFAULT_ACTIVE_CACHE_SECONDS


def test_residency_is_bounded():
    settings = DelegatedCacheSettings.from_connections(
        {"delegated_credentials": {"catalog": {"version_cache_seconds": 10 ** 9}}}
    )
    assert settings.catalog.version_cache_seconds == 24 * 3600

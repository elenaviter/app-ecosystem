# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

from types import SimpleNamespace

from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config,
    oauth_delegated_config_from_connections,
)


def test_config_reads_host_supplied_app_state_without_host_imports():
    source = SimpleNamespace(
        state=SimpleNamespace(
            oauth_delegated_config={
                "enabled": True,
                "tenant": "tenant-a",
                "project": "project-a",
                "resources": [
                    {
                        "resource": "https://example.test/mcp",
                        "grants": ["named_services:use"],
                    }
                ],
            }
        )
    )

    config = oauth_delegated_config(source)

    assert config.enabled is True
    assert config.tenant == "tenant-a"
    assert config.supported_scopes("https://example.test/mcp") == (
        "named_services:use",
    )


def test_catalog_config_parser_never_needs_host_settings():
    config = oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [
                        {"grant": "messages:read", "label": "Read messages"}
                    ],
                }
            }
        }
    )

    assert config.enabled is True
    assert config.supported_scopes() == ("messages:read",)


def test_resource_can_enable_owner_resource_selection_for_oauth_consent():
    config = oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "resources": [
                        {
                            "resource": "https://hub.example.test/mcp/proxy",
                            "grants": ["external_mcp:use"],
                            "resource_selection": True,
                        }
                    ],
                }
            }
        }
    )

    resource = config.resource_config("https://hub.example.test/mcp/proxy")
    assert resource is not None
    assert resource.resource_selection is True

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
                            "selector_type": "kdcube_secret",
                        }
                    ],
                }
            }
        }
    )

    resource = config.resource_config("https://hub.example.test/mcp/proxy")
    assert resource is not None
    assert resource.resource_selection is True
    assert resource.selector_type == "kdcube_secret"


def test_resource_authorization_profiles_expand_against_its_operation_catalog():
    resource_url = "https://runtime.example.test/mcp/problem-board"
    config = oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [
                        {"grant": "work:relay", "label": "Relay"},
                        {"grant": "work:coordinate", "label": "Coordinate"},
                    ],
                    "resources": [
                        {
                            "resource": resource_url,
                            "tools": {
                                "worker.publish": {"grants": ["work:relay"]},
                                "assignment.assign": {"grants": ["work:coordinate"]},
                            },
                            "authorization_profiles": {
                                "worker": {
                                    "scope": "work:profile:worker",
                                    "label": "Problem Board worker",
                                    "operations": ["worker.publish"],
                                },
                                "coordinator": {
                                    "scope": "work:profile:coordinator",
                                    "label": "Problem Board coordinator",
                                    "operations": ["*"],
                                },
                            },
                        }
                    ],
                }
            }
        }
    )

    assert config.supported_scopes(resource_url) == (
        "work:relay",
        "work:coordinate",
        "work:profile:worker",
        "work:profile:coordinator",
    )
    assert config.supported_scopes() == (
        "work:relay",
        "work:coordinate",
        "work:profile:worker",
        "work:profile:coordinator",
    )
    assert config.resource_grants(resource_url) == (
        "work:relay",
        "work:coordinate",
    )
    assert config.authorization_profile("work:profile:worker").label == (
        "Problem Board worker"
    )
    assert [
        tool.name
        for tool in config.tools_for_scopes(
            ["work:profile:worker"],
            resource=resource_url,
        )
    ] == ["worker.publish"]
    assert [
        tool.name
        for tool in config.tools_for_scopes(
            ["work:profile:coordinator"],
            resource=resource_url,
        )
    ] == ["worker.publish", "assignment.assign"]


def test_a_whole_card_row_takes_the_profile_operations_of_every_resource():
    """W272, 2026-09-24: a whole-card consent keys its grants under `*`, and
    `pb worker authorize --replace-card` was refused on save with
    `oauth_authorization_profile_resource_exceeded` for resource `*`."""

    config = oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [
                        {"grant": "work:relay", "label": "Relay"},
                        {"grant": "work:coordinate", "label": "Coordinate"},
                    ],
                    "resources": [
                        {
                            "resource": "https://runtime.example.test/mcp/problem-board",
                            "tools": {
                                "worker.publish": {"grants": ["work:relay"]},
                                "assignment.assign": {"grants": ["work:coordinate"]},
                            },
                            "authorization_profiles": {
                                "worker": {
                                    "scope": "work:profile:worker",
                                    "label": "Problem Board worker",
                                    "operations": ["worker.publish"],
                                },
                            },
                        }
                    ],
                }
            }
        }
    )
    for key in ("*", ""):
        tools = config.authorization_profile_tools(["work:profile:worker"], resource=key)
        assert [tool.name for tool in tools] == ["worker.publish"]
    # A named resource that is not in the catalog still gets nothing.
    assert config.authorization_profile_tools(
        ["work:profile:worker"], resource="https://elsewhere.example/mcp"
    ) == ()

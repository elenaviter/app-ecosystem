from __future__ import annotations

from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.delegated_credentials.oauth.consent import (
    platform_edge_grants_for_scopes,
    requested_card_selection,
    resource_selection_rows,
)


GRANT = "work:relay"
WORKER_PROFILE = "work:profile:worker"
COORDINATOR_PROFILE = "work:profile:coordinator"
GATEWAY_PATTERN = "*/api/bundles/*/connection-hub/public/mcp/gateway*"
GATEWAY_URL = "https://runtime.example.test/api/bundles/demo/connection-hub/public/mcp/gateway"
BOARD_PATTERN = "*/api/bundles/*/problem-board/public/mcp/problem_board*"
BOARD_URL = "https://runtime.example.test/api/bundles/demo/problem-board/public/mcp/problem_board"


def _config(resources: list[dict]):
    return oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [{"grant": GRANT, "label": "Relay work"}],
                    "resources": resources,
                }
            }
        }
    )


def _tool() -> dict:
    return {
        "work.search": {
            "label": "Search work",
            "grants": [GRANT],
        }
    }


def test_consent_offers_one_declared_door_when_catalog_also_has_a_concrete_url() -> None:
    config = _config(
        [
            {
                "resource": GATEWAY_PATTERN,
                "grants": [GRANT],
                "resource_selection": True,
            },
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "grants": [GRANT],
                "tools": _tool(),
            },
            {
                "resource": BOARD_URL,
                "label": "Problem Board on this host",
                "grants": [GRANT],
                "tools": _tool(),
            },
        ]
    )

    rows = resource_selection_rows(
        [GRANT],
        config=config,
        resource=GATEWAY_URL,
        seeded_operations={BOARD_URL: ["work.search"]},
    )

    assert [row["resource"] for row in rows] == [BOARD_PATTERN]
    assert rows[0]["label"] == "Problem Board"
    assert rows[0]["literal"] is False
    assert rows[0]["operations"][0]["held"] is True


def test_consent_does_not_offer_the_concrete_request_again_as_a_child() -> None:
    config = _config(
        [
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "grants": [GRANT],
                "resource_selection": True,
                "tools": _tool(),
            },
            {
                "resource": BOARD_URL,
                "label": "Problem Board on this host",
                "grants": [GRANT],
                "tools": _tool(),
            },
        ]
    )

    rows = resource_selection_rows([GRANT], config=config, resource=BOARD_URL)

    assert rows == []


def test_consent_keeps_an_uncovered_exact_resource_and_marks_it_literal() -> None:
    connector = "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"
    config = _config(
        [
            {
                "resource": GATEWAY_PATTERN,
                "grants": [GRANT],
                "resource_selection": True,
            },
            {
                "resource": connector,
                "label": "Configured MCP server",
                "grants": [GRANT],
                "tools": _tool(),
            },
        ]
    )

    rows = resource_selection_rows([GRANT], config=config, resource=GATEWAY_URL)

    assert [row["resource"] for row in rows] == [connector]
    assert rows[0]["literal"] is True
    assert rows[0]["label"] == "Configured MCP server (literal)"


def test_first_full_card_consent_proposes_every_operation_covered_by_requested_grants() -> None:
    config = _config(
        [
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "grants": [GRANT],
                "tools": {
                    "worker.receive": {
                        "label": "Receive work",
                        "grants": [GRANT],
                    },
                    "admin.delete": {
                        "label": "Delete project",
                        "grants": ["work:admin"],
                    },
                },
            },
            {
                "resource": GATEWAY_PATTERN,
                "label": "Other service",
                "grants": ["other:use"],
                "tools": {
                    "other.call": {
                        "label": "Call other service",
                        "grants": ["other:use"],
                    },
                },
            },
        ]
    )

    selection = requested_card_selection(
        [GRANT],
        config=config,
        full_catalog=True,
    )

    assert selection == {
        "resource_grants": {BOARD_PATTERN: [GRANT]},
        "resource_operations": {BOARD_PATTERN: ["worker.receive"]},
        "named_service_operations": {},
    }


def test_first_entry_consent_uses_the_declared_selector_for_its_proposal() -> None:
    config = _config(
        [{
            "resource": BOARD_PATTERN,
            "label": "Problem Board",
            "grants": [GRANT],
            "tools": _tool(),
        }]
    )

    selection = requested_card_selection(
        [GRANT],
        config=config,
        resource=BOARD_URL,
    )

    assert selection["resource_grants"] == {BOARD_PATTERN: [GRANT]}
    assert selection["resource_operations"] == {BOARD_PATTERN: ["work.search"]}


def test_worker_profile_proposes_only_its_declared_operations_and_real_grants() -> None:
    config = _config(
        [
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "grants": [GRANT],
                "tools": {
                    "worker.publish": {
                        "label": "Publish worker",
                    },
                    "assignment.assign": {
                        "label": "Assign work",
                        "grants": ["work:coordinate"],
                    },
                },
                "authorization_profiles": {
                    "worker": {
                        "scope": WORKER_PROFILE,
                        "label": "Problem Board worker",
                        "operations": ["worker.publish"],
                    },
                    "coordinator": {
                        "scope": COORDINATOR_PROFILE,
                        "label": "Problem Board coordinator",
                        "operations": ["*"],
                    },
                },
            },
            {
                "resource": GATEWAY_PATTERN,
                "label": "Other service",
                "grants": ["other:use"],
                "tools": {
                    "other.call": {
                        "label": "Call other service",
                        "grants": ["other:use"],
                    }
                },
            },
        ]
    )

    selection = requested_card_selection(
        [WORKER_PROFILE],
        config=config,
        full_catalog=True,
    )

    assert selection == {
        "resource_grants": {BOARD_PATTERN: [GRANT]},
        "resource_operations": {BOARD_PATTERN: ["worker.publish"]},
        "named_service_operations": {},
    }
    assert platform_edge_grants_for_scopes(
        [WORKER_PROFILE],
        config=config,
    ) == [
        (
            WORKER_PROFILE,
            "Problem Board worker",
            "",
        )
    ]
    assert "assignment.assign" not in selection["resource_operations"][BOARD_PATTERN]


def test_worker_profile_rows_use_resulting_grants_instead_of_the_marker() -> None:
    config = _config(
        [
            {
                "resource": GATEWAY_PATTERN,
                "resource_selection": True,
            },
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "grants": [GRANT, "work:coordinate"],
                "tools": {
                    "worker.publish": {
                        "label": "Publish worker",
                        "grants": [GRANT],
                    },
                    "assignment.assign": {
                        "label": "Assign work",
                        "grants": ["work:coordinate"],
                    },
                },
                "authorization_profiles": {
                    "worker": {
                        "scope": WORKER_PROFILE,
                        "label": "Problem Board worker",
                        "operations": ["worker.publish"],
                    }
                },
            },
        ]
    )

    rows = resource_selection_rows(
        [WORKER_PROFILE],
        config=config,
        resource=GATEWAY_URL,
    )

    assert len(rows) == 1
    assert rows[0]["resource"] == BOARD_PATTERN
    assert rows[0]["grants"] == [GRANT]
    assert [operation["name"] for operation in rows[0]["operations"]] == [
        "worker.publish"
    ]


def test_coordinator_profile_proposes_the_whole_resource_catalog() -> None:
    config = _config(
        [
            {
                "resource": BOARD_PATTERN,
                "label": "Problem Board",
                "tools": {
                    "worker.publish": {"grants": [GRANT]},
                    "assignment.assign": {"grants": ["work:coordinate"]},
                },
                "authorization_profiles": {
                    "coordinator": {
                        "scope": COORDINATOR_PROFILE,
                        "label": "Problem Board coordinator",
                        "operations": ["*"],
                    }
                },
            }
        ]
    )

    selection = requested_card_selection(
        [COORDINATOR_PROFILE],
        config=config,
        resource=BOARD_URL,
    )

    assert selection["resource_grants"] == {
        BOARD_PATTERN: [GRANT, "work:coordinate"]
    }
    assert selection["resource_operations"] == {
        BOARD_PATTERN: ["worker.publish", "assignment.assign"]
    }

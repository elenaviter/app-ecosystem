from __future__ import annotations

from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.delegated_credentials.oauth.consent import (
    resource_selection_rows,
)


GRANT = "work:relay"
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

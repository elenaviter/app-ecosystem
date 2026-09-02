# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Project active user-owned connectors into delegated-card resource rows."""

from __future__ import annotations

from typing import Iterable

from connection_hub.delegated_credentials.oauth.config import (
    OAuthDelegatedResourceConfig,
    OAuthDelegatedToolConfig,
)
from connection_hub.remote_mcp.models import (
    CONNECTOR_ACTIVE,
    RemoteMCPConnector,
)
from connection_hub.remote_mcp.proxy import EXTERNAL_MCP_GRANT


def remote_mcp_resource_rows(
    connectors: Iterable[RemoteMCPConnector],
) -> tuple[OAuthDelegatedResourceConfig, ...]:
    rows: list[OAuthDelegatedResourceConfig] = []
    for connector in connectors or ():
        if connector.state != CONNECTOR_ACTIVE:
            continue
        rows.append(
            OAuthDelegatedResourceConfig(
                resource=connector.resource,
                label=f"External MCP: {connector.label}",
                identity_scope="grantor",
                grants=(EXTERNAL_MCP_GRANT,),
                tools=tuple(
                    OAuthDelegatedToolConfig(
                        name=tool.name,
                        label=tool.name,
                        description=tool.description,
                        grants=(EXTERNAL_MCP_GRANT,),
                    )
                    for tool in connector.tools
                ),
            )
        )
    rows.sort(key=lambda item: (item.label.lower(), item.resource))
    return tuple(rows)


__all__ = ["remote_mcp_resource_rows"]

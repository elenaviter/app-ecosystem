# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Project active user-owned connectors into delegated-card resource rows.

Each row carries the connector's own descriptor evidence (its descriptor
revision, digest, and one digest per discovered tool) so a card can accept the
connector as its authority and judge drift against the connector rather than
against the deployment catalog version, which the connector does not follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from connection_hub.delegated_credentials.oauth.config import (
    OAuthDelegatedResourceConfig,
    OAuthDelegatedToolConfig,
)
from connection_hub.remote_mcp.models import (
    CONNECTOR_ACTIVE,
    RemoteMCPConnector,
)
from connection_hub.remote_mcp.proxy import EXTERNAL_MCP_GRANT

REMOTE_MCP_RESOURCE_KIND = "remote_mcp"


@dataclass(frozen=True)
class RemoteMCPResourceRow(OAuthDelegatedResourceConfig):
    """A connector as a catalog row, plus the descriptor authority it answers to.

    The attribute names are the ones the card acceptance reader looks for
    (``connection_hub.delegated_credentials.catalog.descriptors``); the
    deployment catalog version is not this row's revision.
    """

    descriptor_kind: str = REMOTE_MCP_RESOURCE_KIND
    descriptor_provider: str = REMOTE_MCP_RESOURCE_KIND
    descriptor_revision: str = ""
    descriptor_digest: str = ""
    operation_digests: Mapping[str, str] = field(default_factory=dict)
    connector_id: str = ""
    connector_revision: int = 0


def remote_mcp_resource_row(connector: RemoteMCPConnector) -> RemoteMCPResourceRow:
    return RemoteMCPResourceRow(
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
        descriptor_revision=str(connector.descriptor_revision),
        descriptor_digest=connector.descriptor_digest,
        operation_digests={tool.name: tool.descriptor_digest for tool in connector.tools},
        connector_id=connector.connector_id,
        connector_revision=connector.revision,
    )


def remote_mcp_resource_rows(
    connectors: Iterable[RemoteMCPConnector],
) -> tuple[RemoteMCPResourceRow, ...]:
    rows: list[RemoteMCPResourceRow] = []
    for connector in connectors or ():
        if connector.state != CONNECTOR_ACTIVE:
            continue
        rows.append(remote_mcp_resource_row(connector))
    rows.sort(key=lambda item: (item.label.lower(), item.resource))
    return tuple(rows)


__all__ = [
    "REMOTE_MCP_RESOURCE_KIND",
    "RemoteMCPResourceRow",
    "remote_mcp_resource_row",
    "remote_mcp_resource_rows",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Project active A2A connectors into delegated-card resource rows.

Each row carries its pinned card's evidence, the descriptor revision, the card
digest and one digest per operation, so a Card accepts the connector as its
authority and judges drift against the pinned card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from connection_hub.delegated_credentials.oauth.config import (
    OAuthDelegatedResourceConfig,
    OAuthDelegatedToolConfig,
)
from connection_hub.remote_a2a.models import (
    CANCEL_TASK_OPERATION,
    EXTERNAL_A2A_GRANT,
    GET_TASK_OPERATION,
    REPLY_TASK_OPERATION,
    RemoteA2AConnector,
    send_operation,
)
from connection_hub.remote_mcp.models import CONNECTOR_ACTIVE

REMOTE_A2A_RESOURCE_KIND = "remote_a2a"
_TASK_OPERATION_TEXT = {
    GET_TASK_OPERATION: ("Read a task", "Read the state and results of a task this Card sent."),
    CANCEL_TASK_OPERATION: ("Cancel a task", "Cancel a task this Card sent."),
    REPLY_TASK_OPERATION: ("Answer a task", "Answer a task this Card sent that is waiting for input."),
}


@dataclass(frozen=True)
class RemoteA2AResourceRow(OAuthDelegatedResourceConfig):
    descriptor_kind: str = REMOTE_A2A_RESOURCE_KIND
    descriptor_provider: str = REMOTE_A2A_RESOURCE_KIND
    descriptor_revision: str = ""
    descriptor_digest: str = ""
    operation_digests: Mapping[str, str] = field(default_factory=dict)
    connector_id: str = ""
    connector_revision: int = 0


def remote_a2a_resource_row(connector: RemoteA2AConnector) -> RemoteA2AResourceRow:
    card = connector.card
    tools = []
    for skill in card.skills if card is not None else ():
        tools.append(
            OAuthDelegatedToolConfig(
                name=send_operation(skill.skill_id),
                label=f"Ask: {skill.name}",
                description=skill.description,
                grants=(EXTERNAL_A2A_GRANT,),
            )
        )
    for operation, (label, description) in _TASK_OPERATION_TEXT.items():
        tools.append(
            OAuthDelegatedToolConfig(
                name=operation, label=label, description=description, grants=(EXTERNAL_A2A_GRANT,)
            )
        )
    return RemoteA2AResourceRow(
        resource=connector.resource,
        label=f"External agent: {connector.label}",
        identity_scope="grantor",
        grants=(EXTERNAL_A2A_GRANT,),
        tools=tuple(tools),
        descriptor_revision=str(connector.descriptor_revision),
        descriptor_digest=connector.card_digest,
        operation_digests=connector.operation_digests(),
        connector_id=connector.connector_id,
        connector_revision=connector.revision,
    )


def remote_a2a_resource_rows(connectors: Iterable[RemoteA2AConnector]) -> tuple[RemoteA2AResourceRow, ...]:
    rows = [remote_a2a_resource_row(item) for item in connectors or () if item.state == CONNECTOR_ACTIVE]
    rows.sort(key=lambda item: (item.label.lower(), item.resource))
    return tuple(rows)


__all__ = [
    "REMOTE_A2A_RESOURCE_KIND",
    "RemoteA2AResourceRow",
    "remote_a2a_resource_row",
    "remote_a2a_resource_rows",
]

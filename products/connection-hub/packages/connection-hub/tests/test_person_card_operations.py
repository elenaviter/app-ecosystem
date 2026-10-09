"""W667: an application declares the operations a PERSON's Card decides on its resource row.

Operator, 2026-10-09: "eveyrthing that is not controlled and cannot be controlled must be hidden!". Connection
Hub stays generic: it reads the declared ``person_card_operations`` (only the row's own operations, in catalog
order), serves it with the resource option, and a person's Card editor hides the rest. Presentation only:
the declaration is outside the row digest and never edits a Card.
"""
from __future__ import annotations

import asyncio
import copy

from connection_hub.delegated_credentials.catalog.descriptors import resource_row_digest
from test_managed_grants import USER, _connections, _row
from test_oauth_dcr_client_independence import DECLARED_RESOURCE, _GrantStore, _Persistence, _service


def _declared(operations):
    connections = copy.deepcopy(_connections(managed=None))
    row = connections["delegated_credentials"]["oauth"]["resources"][0]
    if operations is not None:
        row["person_card_operations"] = list(operations)
    return connections


def test_the_declaration_names_only_the_rows_own_operations_in_catalog_order():
    assert _row(_declared(None)).person_card_operations == ()
    assert _row(_declared(["item.steward", "item.read", "item.unknown"])).person_card_operations == (
        "item.read", "item.steward")


def test_the_declaration_is_presentation_outside_the_row_digest():
    assert resource_row_digest(_row(_declared(["item.read"]))) == resource_row_digest(_row(_declared(None)))


def test_the_resource_option_serves_the_declaration_only_when_declared():
    async def option(connections):
        service = _service(_GrantStore({}), _Persistence(), connections=connections)
        return next(item for item in await service.resource_options(USER) if item["resource"] == DECLARED_RESOURCE)

    assert asyncio.run(option(_declared(["item.read"])))["person_card_operations"] == ["item.read"]
    assert "person_card_operations" not in asyncio.run(option(_declared(None)))

"""W748: an application declares the operations every member holds by membership, on its resource row.

Operator, live 2026-10-11 about 17:46Z: "read is not ticked btw". A person's Card editor shows an operation
that Card does not decide ticked when it is in effect for the person: the declared ``person_member_operations``
(only the row's own operations, in catalog order, never one a person's Card decides). Presentation only: the
declaration is outside the row digest and never edits a Card.
"""
from __future__ import annotations

import asyncio
import copy

from connection_hub.delegated_credentials.catalog.descriptors import resource_row_digest
from test_managed_grants import USER, _connections, _row
from test_oauth_dcr_client_independence import DECLARED_RESOURCE, _GrantStore, _Persistence, _service


def _declared(members, decided=None):
    connections = copy.deepcopy(_connections(managed=None))
    row = connections["delegated_credentials"]["oauth"]["resources"][0]
    if members is not None:
        row["person_member_operations"] = list(members)
    if decided is not None:
        row["person_card_operations"] = list(decided)
    return connections


def test_the_declaration_names_only_the_rows_own_operations_in_catalog_order():
    assert _row(_declared(None)).person_member_operations == ()
    assert _row(_declared(["item.steward", "item.read", "item.unknown"])).person_member_operations == (
        "item.read", "item.steward")


def test_an_operation_a_persons_card_decides_is_never_a_membership_operation():
    row = _row(_declared(["item.read", "item.steward"], decided=["item.steward"]))
    assert row.person_member_operations == ("item.read",) and row.person_card_operations == ("item.steward",)


def test_the_declaration_is_presentation_outside_the_row_digest():
    assert resource_row_digest(_row(_declared(["item.read"]))) == resource_row_digest(_row(_declared(None)))


def test_the_resource_option_serves_the_declaration_only_when_declared():
    async def option(connections):
        service = _service(_GrantStore({}), _Persistence(), connections=connections)
        return next(item for item in await service.resource_options(USER) if item["resource"] == DECLARED_RESOURCE)

    assert asyncio.run(option(_declared(["item.read"])))["person_member_operations"] == ["item.read"]
    assert "person_member_operations" not in asyncio.run(option(_declared(None)))

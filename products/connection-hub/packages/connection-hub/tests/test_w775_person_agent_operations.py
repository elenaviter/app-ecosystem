"""W775: an application declares the operations only an agent's own channel calls, on its resource row.

Operator, 2026-10-13 (about 06:40Z): "(b) on a person's Card in Connection Hub, fold the agent-only tools under
\"Agent-only tools (not for people)\" without the amber INACTIVE warning. << b) - yes." The declared
``person_agent_operations`` (only the row's own operations, in catalog order, never one a person's Card decides or
a member holds) is what a person's Card editor folds away. Presentation only: outside the row digest, never a
selection or save rule.
"""
from __future__ import annotations

import asyncio
import copy

from connection_hub.delegated_credentials.catalog.descriptors import resource_row_digest
from test_managed_grants import USER, _connections, _row
from test_oauth_dcr_client_independence import DECLARED_RESOURCE, _GrantStore, _Persistence, _service


def _declared(agents, decided=None, members=None):
    connections = copy.deepcopy(_connections(managed=None))
    row = connections["delegated_credentials"]["oauth"]["resources"][0]
    if agents is not None:
        row["person_agent_operations"] = list(agents)
    if decided is not None:
        row["person_card_operations"] = list(decided)
    if members is not None:
        row["person_member_operations"] = list(members)
    return connections


def test_the_declaration_names_only_the_rows_own_operations_in_catalog_order():
    assert _row(_declared(None)).person_agent_operations == ()
    assert _row(_declared(["item.steward", "item.read", "item.unknown"])).person_agent_operations == (
        "item.read", "item.steward")


def test_an_operation_a_person_may_call_is_never_agent_only():
    row = _row(_declared(["item.read", "item.steward"], decided=["item.steward"]))
    assert row.person_agent_operations == ("item.read",)
    row = _row(_declared(["item.read", "item.steward"], members=["item.read"]))
    assert row.person_agent_operations == ("item.steward",)


def test_the_declaration_is_presentation_outside_the_row_digest():
    assert resource_row_digest(_row(_declared(["item.read"]))) == resource_row_digest(_row(_declared(None)))


def test_the_resource_option_serves_the_declaration_only_when_declared():
    async def option(connections):
        service = _service(_GrantStore({}), _Persistence(), connections=connections)
        return next(item for item in await service.resource_options(USER) if item["resource"] == DECLARED_RESOURCE)

    assert asyncio.run(option(_declared(["item.read"])))["person_agent_operations"] == ["item.read"]
    assert "person_agent_operations" not in asyncio.run(option(_declared(None)))

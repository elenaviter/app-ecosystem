# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W918: only the Card's owner adds or changes their custom MCP connectors; anyone may remove one."""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.owner_only_connectors import (
    CUSTOM_MCP_OWNER_ONLY, custom_mcp_owner_only_refusal,
)

OWNER = "user:owner"
ADMIN = "user:admin"
PB = "https://board.example.test/mcp"
A = "urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa"
B = "urn:connection-hub:remote-mcp:mcp_bbbbbbbbbbbbbbbbbbbbbbbb"


def card(connectors=None, *, pb_ops=("plan.read",)):
    connectors = {A: ("search",)} if connectors is None else connectors
    return CardAuthority(
        access_id="agent-card", client_id="agent-client", grantor_subject=OWNER, delegate_subject="agent",
        source="manual", card_kind="agent", card_revision=3,
        resource_grants={PB: ("work:use",), **{c: ("external_mcp:use",) for c in connectors}},
        resource_operations={PB: tuple(pb_ops), **{c: tuple(t) for c, t in connectors.items()}},
        named_service_operations=NamedServiceSelection.exact({}), identity_scope="grantor",
        created_at=1, expires_at=2000000000,
    )


def refusal(original, candidate, actor=ADMIN, **extra):
    return custom_mcp_owner_only_refusal(original=original, candidate=candidate, actor_subject=actor, **extra)


def test_the_owner_may_do_anything_with_their_connectors():
    assert refusal(card(), card({A: ("search", "delete"), B: ("x",)}), actor=OWNER) is None
    assert refusal(card(), card({}), actor=OWNER, invocation_modes={A: {"search": "always"}}) is None


def test_an_admin_edit_that_leaves_connectors_unchanged_passes():
    assert refusal(card(), card(pb_ops=("plan.read", "plan.write"))) is None


def test_an_admin_may_remove_a_whole_connector():
    both = card({A: ("search",), B: ("fetch",)})
    assert refusal(both, card({A: ("search",)})) is None
    assert refusal(both, card({})) is None


@pytest.mark.parametrize("candidate", [
    card({A: ("search",), B: ("fetch",)}),   # adds a connector
    card({A: ("search", "delete")}),         # changes the tools of one that stays
    card({A: ()}),                           # empties the tools of one that stays
])
def test_an_admin_may_not_add_or_change_a_connector(candidate):
    assert refusal(card(), candidate) == CUSTOM_MCP_OWNER_ONLY


def test_an_admin_may_not_change_a_connectors_grants():
    changed = card()
    changed = dataclasses.replace(changed, resource_grants={**changed.resource_grants, A: ("external_mcp:admin",)})
    assert refusal(card(), changed) == CUSTOM_MCP_OWNER_ONLY


@pytest.mark.parametrize("carrier", ["invocation_modes", "accepted_operations"])
def test_an_admin_may_not_set_a_connectors_policy_or_acceptance(carrier):
    assert refusal(card(), card(), **{carrier: {A: {"search": "always"}}}) == CUSTOM_MCP_OWNER_ONLY
    assert refusal(card(), card(), **{carrier: {PB: {"plan.read": "always"}}}) is None


def test_the_owner_is_the_stored_grantor_not_the_request():
    original = card()
    claimed = dataclasses.replace(card({A: ("search", "delete")}), grantor_subject=ADMIN)
    # The candidate claiming ADMIN as grantor does not make ADMIN the owner.
    assert refusal(original, claimed, actor=ADMIN) == CUSTOM_MCP_OWNER_ONLY

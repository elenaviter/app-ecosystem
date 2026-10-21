# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W918: a project's External MCP switch over its members' own connectors."""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CardAuthority, CardRecordError, ControlCardBinding, NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls import project_mcp_family as family_module
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer, new_credentialless_card
from connection_hub.delegated_credentials.controls.project_mcp_family import (
    EXTERNAL_MCP_FAMILY_PROPERTY, configure_project_resource_families, project_family_gate,
)

PROJECT = "work:project:w918-test"
CREATOR = "user:creator"
OWNER = "user:owner"
PB = "https://board.example.test/mcp"
CONNECTOR_A = "urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa"
CONNECTOR_B = "urn:connection-hub:remote-mcp:mcp_bbbbbbbbbbbbbbbbbbbbbbbb"
FAMILY = {
    "id": "user_external_mcp", "resource_kinds": ["remote_mcp"],
    "resource_patterns": ["urn:connection-hub:remote-mcp:*"],
    "allowed_tools": ["*"], "max_resources": 8, "max_tools_per_resource": 64,
}


@pytest.fixture(autouse=True)
def project_family():
    configure_project_resource_families([FAMILY])
    yield
    configure_project_resource_families(None)


def project_control(switch=None, *, selection=(PB,), mode="and"):
    properties = {} if switch is None else {EXTERNAL_MCP_FAMILY_PROPERTY: switch}
    seed = CardAuthority(
        access_id="seed", client_id="seed", grantor_subject=CREATOR, delegate_subject="caller",
        source="manual", card_kind="automation",
        resource_grants={r: ("work:use",) for r in selection},
        resource_operations={r: ("plan.read",) for r in selection},
        named_service_operations=NamedServiceSelection.exact({}), identity_scope="grantor", expires_at=2000000000,
    )
    return new_credentialless_card(
        grantor_subject=CREATOR, catalog_version="catalog",
        control_id=control_card_id_for_issuer("application", PROJECT, grantor_subject=CREATOR),
        issuer_ref=PROJECT, issuer_kind="application", initial_selection=seed,
        composition_mode=mode, properties=properties, now=1,
    )


def agent(parent, *, connectors=(CONNECTOR_A,), tools=("search", "fetch")):
    grants = {PB: ("work:use",), **{c: ("external_mcp:use",) for c in connectors}}
    operations = {PB: ("plan.read",), **{c: tuple(tools) for c in connectors}}
    return CardAuthority(
        access_id="agent-card", client_id="agent-client", grantor_subject=OWNER, delegate_subject="agent",
        source="manual", card_kind="agent", card_revision=3, resource_grants=grants,
        resource_operations=operations, named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", created_at=1, expires_at=2000000000,
        control_card=ControlCardBinding(
            control_id=parent.access_id, issuer_ref=PROJECT, issuer_kind="application",
            control_revision=parent.card_revision, holder_subject=CREATOR,
        ),
    )


async def resolve(card, controls):
    from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy

    by_coordinate = {(c.grantor_subject, c.access_id): c for c in controls}

    async def load_control(control_id, *, grantor_subject):
        return by_coordinate.get((grantor_subject, control_id))

    return (await compose_control_hierarchy(card, load_control=load_control)).effective_card


async def test_switch_on_admits_the_owners_own_connectors_unchanged():
    p = project_control("on")
    effective = await resolve(agent(p, connectors=(CONNECTOR_A, CONNECTOR_B)), [p])
    assert effective.resource_grants[CONNECTOR_A] == ("external_mcp:use",)
    assert effective.resource_operations[CONNECTOR_A] == ("fetch", "search")
    assert CONNECTOR_B in effective.resource_grants
    assert effective.resource_grants[PB] == ("work:use",)


@pytest.mark.parametrize("switch", ["off", None, "ON", "yes", 1])
async def test_switch_off_absent_or_invalid_drops_connectors(switch):
    if switch in ("ON", "yes", 1):
        # The codec refuses an invalid stored value; a Control that somehow
        # carried one could not even be built.
        with pytest.raises(CardRecordError):
            project_control(switch)
        return
    p = project_control(switch)
    effective = await resolve(agent(p), [p])
    assert CONNECTOR_A not in effective.resource_grants
    assert effective.resource_grants[PB] == ("work:use",)


async def test_project_or_mode_never_contributes_or_reopens_connectors():
    p = project_control("off", mode="or")
    # A foreign-holder OR is refused before composition, as today.
    with pytest.raises(ControlCardMismatch):
        await resolve(agent(p), [p])


async def test_unconfigured_family_closes_the_gate():
    configure_project_resource_families(None)
    p = project_control("on")
    effective = await resolve(agent(p), [p])
    assert CONNECTOR_A not in effective.resource_grants


async def test_invalid_family_declaration_configures_nothing():
    configure_project_resource_families([{**FAMILY, "max_resources": 0}])
    assert family_module.configured_family() is None
    p = project_control("on")
    effective = await resolve(agent(p), [p])
    assert CONNECTOR_A not in effective.resource_grants


async def test_family_limits_are_enforced():
    configure_project_resource_families([{**FAMILY, "max_resources": 1}])
    p = project_control("on")
    with pytest.raises(ControlCardMismatch, match="project_resource_family_resource_limit"):
        await resolve(agent(p, connectors=(CONNECTOR_A, CONNECTOR_B)), [p])
    configure_project_resource_families([{**FAMILY, "max_tools_per_resource": 1}])
    with pytest.raises(ControlCardMismatch, match="project_resource_family_tool_limit"):
        await resolve(agent(p, tools=("a", "b")), [p])
    configure_project_resource_families([{**FAMILY, "allowed_tools": ["search"]}])
    effective = await resolve(agent(p, tools=("search", "delete")), [p])
    assert effective.resource_operations[CONNECTOR_A] == ("search",)


async def test_a_project_control_carrying_a_connector_contributes_nothing():
    p = project_control("on", selection=(PB, CONNECTOR_B))
    effective = await resolve(agent(p, connectors=(CONNECTOR_A,)), [p])
    assert CONNECTOR_B not in effective.resource_grants
    assert CONNECTOR_A in effective.resource_grants


async def test_switching_changes_only_the_control_not_the_agent_card():
    off = project_control("off")
    card = agent(off)
    assert CONNECTOR_A not in (await resolve(card, [off])).resource_grants
    on = dataclasses.replace(off, properties={**off.properties, EXTERNAL_MCP_FAMILY_PROPERTY: "on"})
    assert CONNECTOR_A in (await resolve(card, [on])).resource_grants
    assert card.resource_grants[CONNECTOR_A] == ("external_mcp:use",)


async def test_the_switch_is_never_inherited_by_the_effective_card():
    p = project_control("on")
    effective = await resolve(agent(p), [p])
    assert EXTERNAL_MCP_FAMILY_PROPERTY not in effective.properties


def test_a_caller_card_cannot_carry_the_switch():
    p = project_control("on")
    with pytest.raises(CardRecordError, match="external_mcp_family_requires_control_card"):
        dataclasses.replace(agent(p), properties={EXTERNAL_MCP_FAMILY_PROPERTY: "on"})


async def test_a_non_project_chain_keeps_todays_folding():
    p = project_control("on")
    other = dataclasses.replace(p, issuer_ref="work:project:other")
    assert project_family_gate(agent(p), (other,)) is None
    service = new_credentialless_card(
        grantor_subject=OWNER, catalog_version="catalog", control_id="svc", issuer_ref="issuer:svc",
        issuer_kind="service", initial_selection=agent(p), composition_mode="and",
        properties={EXTERNAL_MCP_FAMILY_PROPERTY: "on"}, now=1,
    )
    card = dataclasses.replace(agent(p), control_card=ControlCardBinding(
        control_id=service.access_id, issuer_ref=service.issuer_ref, issuer_kind="service",
        control_revision=service.card_revision))
    assert project_family_gate(card, (service,)) is None
    effective = await resolve(card, [service])
    # Folded by plain AND, exactly as before: the service Control holds the connector too.
    assert effective.resource_grants[CONNECTOR_A] == ("external_mcp:use",)


async def test_a_missing_project_control_still_fails_closed():
    p = project_control("on")
    with pytest.raises(ControlCardMismatch):
        await resolve(agent(p), [])


# --- A person's My Card under their project Control C under P ---------------------------------

def person_chain(p_switch="on", c_switch=None, *, connectors=(CONNECTOR_A,)):
    from connection_hub.delegated_credentials.controls.project_person import (
        ProjectPersonControlIdentity, bind_project_person_control,
    )
    from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card
    from test_project_person_control import PROJECT_REF, RESOURCE, TARGET, _authority

    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    c = bind_project_person_control(_authority(), identity=identity)
    holder = "user:project-creator"
    seed = CardAuthority(
        access_id="seed", client_id="seed", grantor_subject=holder, delegate_subject="caller",
        source="manual", card_kind="automation", resource_grants={RESOURCE: ("work:review",)},
        resource_operations={RESOURCE: ("review.accept",)}, named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", expires_at=2000000000,
    )
    p = new_credentialless_card(
        grantor_subject=holder, catalog_version="catalog-v1",
        control_id=control_card_id_for_issuer("application", PROJECT_REF, grantor_subject=holder),
        issuer_ref=PROJECT_REF, issuer_kind="application", initial_selection=seed, composition_mode="and",
        properties={} if p_switch is None else {EXTERNAL_MCP_FAMILY_PROPERTY: p_switch}, now=1,
    )
    from connection_hub.delegated_credentials.controls.snapshot import CONTROL_SNAPSHOT_PROPERTY

    c = dataclasses.replace(
        c, properties={**c.properties, CONTROL_SNAPSHOT_PROPERTY: p.properties[CONTROL_SNAPSHOT_PROPERTY],
                       **({} if c_switch is None else {EXTERNAL_MCP_FAMILY_PROPERTY: c_switch})},
        control_card=ControlCardBinding(control_id=p.access_id, issuer_ref=PROJECT_REF, issuer_kind="application",
                                        control_revision=p.card_revision, holder_subject=holder))
    my = new_project_person_my_card(control_card=c)
    my = dataclasses.replace(
        my,
        resource_grants={**my.resource_grants, **{x: ("external_mcp:use",) for x in connectors}},
        resource_operations={**my.resource_operations, **{x: ("search",) for x in connectors}},
    )
    return my, c, p, RESOURCE


async def resolve_person(my, c, p):
    return await resolve(my, [c, p])


async def test_person_chain_switch_on_admits_connectors_and_keeps_the_board_fold():
    my, c, p, board = person_chain("on")
    assert project_family_gate(my, (c, p)) is True
    effective = await resolve_person(my, c, p)
    assert effective.resource_grants[CONNECTOR_A] == ("external_mcp:use",)
    assert effective.resource_grants[board] == ("work:review",)


@pytest.mark.parametrize("p_switch,c_switch,admitted", [
    ("on", None, True), ("on", "on", True), ("on", "off", False),
    ("off", "on", False), ("off", None, False), (None, "on", False),
])
async def test_person_chain_c_may_narrow_never_widen(p_switch, c_switch, admitted):
    my, c, p, _ = person_chain(p_switch, c_switch)
    effective = await resolve_person(my, c, p)
    assert (CONNECTOR_A in effective.resource_grants) is admitted


async def test_person_chain_c_carrying_a_connector_contributes_nothing():
    my, c, p, _ = person_chain("on", connectors=())
    c = dataclasses.replace(c, resource_grants={**c.resource_grants, CONNECTOR_B: ("external_mcp:use",)},
                            resource_operations={**c.resource_operations, CONNECTOR_B: ("search",)})
    my = dataclasses.replace(my, control_card=dataclasses.replace(my.control_card, control_revision=c.card_revision))
    effective = await resolve_person(my, c, p)
    assert CONNECTOR_B not in effective.resource_grants


# --- The switch is written only by the project's own P edit ------------------------------------

def test_the_switch_is_an_exact_field_of_a_project_control_body_only():
    from connection_hub.delegated_credentials.managed_card_edit_forward import (
        ManagedCardEditError, managed_card_edit_body,
    )

    def body(kind="project_control", **extra):
        return managed_card_edit_body(actor_subject="alice", project_ref="work:project:p", request_id="r-1",
            kind=kind, principal_key="card:p-1", original_revision=2, access_id="p-1", subject_hash="h",
            selection={}, **extra)

    assert body(external_mcp_family="off")["external_mcp_family"] == "off"
    for bad in ("ON", "", "yes"):
        with pytest.raises(ManagedCardEditError):
            body(external_mcp_family=bad)
    with pytest.raises(ManagedCardEditError):
        body(kind="agent_card", external_mcp_family="on")


def _forward(svc, record, **changes):
    import asyncio

    return asyncio.run(svc._forward_managed_project_control(
        {"user_id": "alice"}, record=record, request_id="family-1",
        changes=dict(expected_card_revision=5, **changes)))


def test_a_changed_switch_travels_signed_through_the_project():
    from test_w638_project_control_forward import Host, project_control as forwarded_p, service

    host = Host()
    result = _forward(service(host), forwarded_p(card_revision=5), external_mcp_family="on")
    assert result["ok"] is True, result
    (_, _, body), = host.calls
    assert body["external_mcp_family"] == "on" and body["selection"] == {}
    assert "service_proof" in body


def test_an_unchanged_switch_answers_ok_without_a_write():
    from test_w638_project_control_forward import Host, project_control as forwarded_p, service

    host = Host()
    stored = forwarded_p(card_revision=5, properties={"kept": "as stored", EXTERNAL_MCP_FAMILY_PROPERTY: "on"})
    result = _forward(service(host, durable=stored), stored, external_mcp_family="on")
    assert result == {"ok": True, "unchanged": True} and host.calls == []


def test_the_planner_sets_the_switch_only_on_the_project_control():
    import asyncio

    from connection_hub.delegated_credentials.managed_card_selection_plan import build_managed_card_selection_update
    from service_foundation.coordination.durable_decision_log import DecisionRefused
    from test_w638_managed_card_selection_plan import PROJECT as PLAN_PROJECT, project_control as stored_p

    class Host:
        async def _resolve_card_authority(self, **kwargs):
            pytest.fail("an invalid switch must refuse before any resolution")

    for kind, value in (("agent_card", "on"), ("project_control", "maybe")):
        with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
            asyncio.run(build_managed_card_selection_update(Host(), original=stored_p(), kind=kind, selection={},
                active=None, decision=None, project_ref=PLAN_PROJECT, actor_subject="alice", request_id="plan-1",
                now=1_800_000_000, external_mcp_family=value))


async def test_the_effective_project_control_never_shows_a_connector():
    from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy

    my, c, p, _ = person_chain("on", connectors=())
    p = dataclasses.replace(p, composition_mode="or",
                            resource_grants={**p.resource_grants, CONNECTOR_B: ("external_mcp:use",)},
                            resource_operations={**p.resource_operations, CONNECTOR_B: ("search",)})
    by = {(x.grantor_subject, x.access_id): x for x in (c, p)}

    async def load_control(control_id, *, grantor_subject):
        return by.get((grantor_subject, control_id))

    hierarchy = await compose_control_hierarchy(my, load_control=load_control)
    assert CONNECTOR_B not in hierarchy.effective_control_card.resource_grants
    assert CONNECTOR_B not in hierarchy.effective_card.resource_grants

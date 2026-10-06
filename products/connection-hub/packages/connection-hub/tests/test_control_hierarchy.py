# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Current parent authority composes without rewriting a person's selection."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CardAuthority, ControlCardBinding, NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.model import new_credentialless_card

RESOURCE = "https://service.example.test/mcp"
OTHER = "https://other.example.test/mcp"


def control(card_id, selection, mode="and", *, properties=None):
    initial = CardAuthority(
        access_id="seed", client_id="seed", grantor_subject="owner",
        delegate_subject="caller", source="manual", card_kind="automation",
        resource_grants={resource: ("read",) for resource in selection},
        resource_operations={resource: ("inspect",) for resource in selection},
        named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", expires_at=2000000000,
    )
    return new_credentialless_card(
        grantor_subject="owner", catalog_version="catalog", control_id=card_id,
        issuer_ref=f"issuer:{card_id}", issuer_kind="service", initial_selection=initial,
        composition_mode=mode, properties=properties, now=1,
    )


def binding(parent):
    return ControlCardBinding(
        control_id=parent.access_id, issuer_ref=parent.issuer_ref,
        issuer_kind=parent.issuer_kind, control_revision=parent.card_revision,
    )


def caller(selection, parent):
    return CardAuthority(
        access_id="person", client_id="person-client", grantor_subject="owner",
        delegate_subject="person-caller", source="manual", card_kind="automation",
        card_revision=7, resource_grants={r: ("read",) for r in selection},
        resource_operations={r: ("inspect",) for r in selection},
        named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", created_at=1, expires_at=2000000000,
        control_card=binding(parent),
    )


async def resolve(card, controls):
    from connection_hub.delegated_credentials.controls.hierarchy import (
        compose_control_hierarchy,
    )

    async def load_control(control_id, *, grantor_subject):
        return controls.get((grantor_subject, control_id))

    return await compose_control_hierarchy(card, load_control=load_control)


# Two edge modes plus the three stored selections give exactly 32 cases.
MATRIX = list(itertools.product(("and", "or"), ("and", "or"), (False, True), (False, True), (False, True)))


@pytest.mark.parametrize("parent_mode,child_mode,p,c,m", MATRIX)
async def test_all_32_current_parent_control_my_compositions(parent_mode, child_mode, p, c, m):
    parent = control("parent", [RESOURCE] if p else [], parent_mode)
    child = dataclasses.replace(
        control("child", [RESOURCE] if c else [], child_mode),
        control_card=binding(parent),
    )
    person = caller([RESOURCE] if m else [], child)
    originals = (parent.to_dict(), child.to_dict(), person.to_dict())
    result = await resolve(person, {("owner", "parent"): parent, ("owner", "child"): child})
    downstream = (c and m) if child_mode == "and" else (c or m)
    expected = (p and downstream) if parent_mode == "and" else (p or downstream)
    assert (RESOURCE in result.effective_card.resource_grants) is expected
    assert result.effective_card.access_id == person.access_id
    assert result.effective_card.client_id == person.client_id
    assert result.effective_card.delegate_subject == person.delegate_subject
    assert result.effective_card.expires_at == person.expires_at
    assert result.effective_card.card_revision == 7
    assert (parent.to_dict(), child.to_dict(), person.to_dict()) == originals


async def test_modes_are_current_upstream_owned_and_per_service():
    parent = control(
        "parent", [RESOURCE, OTHER], properties={
            "service_composition_modes": {RESOURCE: "or", OTHER: "and"},
        },
    )
    child = dataclasses.replace(
        control("child", [OTHER], properties={
            "service_composition_modes": {RESOURCE: "or", OTHER: "and"},
        }),
        control_card=binding(parent),
    )
    person = caller([], child)
    rows = {("owner", "parent"): parent, ("owner", "child"): child}
    result = await resolve(person, rows)
    assert set(result.effective_card.resource_grants) == {RESOURCE}
    # A changed CURRENT upstream Card immediately changes the decision.
    rows[("owner", "parent")] = dataclasses.replace(
        parent, card_revision=2, resource_grants={}, resource_operations={},
    )
    second = await resolve(person, rows)
    assert not second.effective_card.resource_grants
    assert person.resource_grants == {}
    assert person.card_revision == 7


@pytest.mark.parametrize("failure", ("missing", "revoked", "cycle", "foreign", "unavailable"))
async def test_parent_dependency_failure_never_falls_open(failure):
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch

    parent = control("parent", [RESOURCE], "or")
    child = dataclasses.replace(control("child", [], "or"), control_card=binding(parent))
    person = caller([], child)
    rows = {("owner", "parent"): parent, ("owner", "child"): child}
    if failure == "missing":
        rows.pop(("owner", "parent"))
    elif failure == "revoked":
        rows[("owner", "parent")] = dataclasses.replace(parent, state="revoked")
    elif failure == "cycle":
        rows[("owner", "parent")] = dataclasses.replace(parent, control_card=binding(child))
    elif failure == "foreign":
        rows[("owner", "parent")] = dataclasses.replace(parent, grantor_subject="another")
    if failure == "unavailable":
        from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy

        async def unavailable(_control_id, *, grantor_subject):
            raise RuntimeError("storage unavailable")

        with pytest.raises(ControlCardMismatch):
            await compose_control_hierarchy(person, load_control=unavailable)
    else:
        with pytest.raises(ControlCardMismatch):
            await resolve(person, rows)


async def test_three_levels_cannot_reopen_an_ancestor_and_ceiling():
    parent = control("parent", [], "and")
    middle = dataclasses.replace(control("middle", [], "or"), control_card=binding(parent))
    child = dataclasses.replace(control("child", [], "or"), control_card=binding(middle))
    person = caller([RESOURCE], child)
    result = await resolve(person, {("owner", c.access_id): c for c in (parent, middle, child)})
    assert not result.effective_card.resource_grants
    assert person.resource_grants == {RESOURCE: ("read",)}


async def test_upstream_or_combines_with_everything_downstream():
    parent = dataclasses.replace(control("parent", [RESOURCE], "or"), card_revision=5)
    child = dataclasses.replace(control("child", [], "and"), card_revision=2, control_card=binding(parent))
    person = caller([], child)
    result = await resolve(person, {("owner", "parent"): parent, ("owner", "child"): child})
    assert RESOURCE in result.effective_card.resource_grants
    assert result.effective_card.control_card.control_id == child.access_id
    assert result.effective_card.control_card.control_revision == child.card_revision


async def test_dependency_changing_during_resolution_is_refused():
    from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch

    parent = control("parent", [RESOURCE], "or")
    reads = 0

    async def changed(_control_id, *, grantor_subject):
        nonlocal reads
        reads += 1
        return parent if reads == 1 else dataclasses.replace(parent, card_revision=2)

    with pytest.raises(ControlCardMismatch, match="control_card_dependency_changed"):
        await compose_control_hierarchy(caller([], parent), load_control=changed)


@pytest.mark.parametrize("parent_mode,child_mode,p,c,m", MATRIX)
def test_actual_human_evaluator_all_32_compositions(parent_mode, child_mode, p, c, m):
    import test_project_identity_authorization as human
    from connection_hub.delegated_credentials.project_identity_authorization import (
        ProjectCardResolution, ProjectOperationRequest, authorize_project_operation,
    )

    parent = dataclasses.replace(
        control("parent", [], parent_mode), grantor_subject=human.PROJECT_SUBJECT,
        resource_grants={human.RESOURCE: (human.GRANT,)} if p else {},
        resource_operations={human.RESOURCE: (human.OPERATION,)} if p else {},
    )
    child = dataclasses.replace(
        human._control_card(operations=(human.OPERATION,) if c else (), grants=(human.GRANT,) if c else ()),
        composition_mode=child_mode, control_card=binding(parent),
    )
    person = human._my_card(operations=(human.OPERATION,) if m else (), grants=(human.GRANT,) if m else ())
    decision = authorize_project_operation(
        request=ProjectOperationRequest(person_subject=human.PERSON, project_ref=human.PROJECT_REF,
                                       resource=human.RESOURCE, operation=human.OPERATION,
                                       required_grants=(human.GRANT,)),
        edge=human._edge(child, person), catalog=human._catalog(), now=human.NOW,
        control_card=ProjectCardResolution.current(child, control_dependencies=(parent,)),
        my_card=ProjectCardResolution.current(person),
    )
    downstream = (c and m) if child_mode == "and" else (c or m)
    expected = (p and downstream) if parent_mode == "and" else (p or downstream)
    assert decision.allowed is expected, decision


async def test_per_service_union_cannot_bypass_foreign_holder_guard():
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch

    parent = dataclasses.replace(control("parent", [RESOURCE], properties={
        "service_composition_modes": {RESOURCE: "or"}}), grantor_subject="foreign")
    person = dataclasses.replace(caller([], parent), control_card=dataclasses.replace(binding(parent), holder_subject="foreign"))
    with pytest.raises(ControlCardMismatch, match="control_card_foreign_holder_requires_and"):
        await resolve(person, {("foreign", "parent"): parent})


async def test_named_services_inherit_their_resource_mode():
    parent = dataclasses.replace(control("parent", [RESOURCE, OTHER], properties={
        "service_composition_modes": {RESOURCE: "or", OTHER: "and"}}),
        named_service_operations=NamedServiceSelection.exact({
            RESOURCE: {"notes": ("inspect",)}, OTHER: {"notes": ("inspect",)}}),
        named_services={"namespaces": {"notes": {"tools": {
            "inspect": {"operations": {"inspect": {"grants": []}}}}}}})
    person = caller([], parent)
    result = await resolve(person, {("owner", "parent"): parent})
    assert result.effective_card.named_service_operations.operations == {
        RESOURCE: {"notes": ("inspect",)}}


async def test_bounded_depth_is_enforced_even_without_a_cycle():
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
    parent = control("root", [RESOURCE], "or")
    rows = {("owner", parent.access_id): parent}
    for index in range(33):
        parent = dataclasses.replace(control(f"child-{index}", [], "or"), control_card=binding(parent))
        rows[("owner", parent.access_id)] = parent
    with pytest.raises(ControlCardMismatch, match="control_card_chain_too_deep"):
        await resolve(caller([], parent), rows)


@pytest.mark.parametrize("invalid", (None, [], {RESOURCE: "xor"}, {RESOURCE: []}, {"": "and"}, {" padded ": "and"}))
def test_invalid_upstream_mode_maps_are_not_normalized_open(invalid):
    from connection_hub.delegated_credentials.cards.model import CardRecordError
    with pytest.raises(CardRecordError, match="control_card_service_composition_modes_invalid"):
        control("parent", [], properties={"service_composition_modes": invalid})


@pytest.mark.parametrize("failure", (None, "missing", "cycle"))
async def test_real_attach_validates_the_entire_chain_before_persist(failure):
    from unittest.mock import AsyncMock
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from test_credentialless_card_persistence import _persistence, _Handles

    parent = control("parent", [RESOURCE])
    child = control("child", [RESOURCE], "or")
    if failure == "missing":
        parent = dataclasses.replace(parent, control_card=binding(control("missing", [])))
    elif failure == "cycle":
        parent = dataclasses.replace(parent, control_card=binding(child))
    rows = {c.access_id: c for c in (parent, child)}
    service = AutomationAccessService(redis=object(), tenant="test", project="test",
        config=None, grant_store=object(), card_persistence=_persistence(rows, _Handles({})))
    service.notify_change = AsyncMock()
    outcome = await service.attach_control_card({"user_id": "owner"},
        access_id=child.access_id, control_id=parent.access_id, expected_card_revision=1)
    assert outcome["ok"] is (failure is None), outcome
    if failure is None:
        assert rows[child.access_id].control_card.control_id == parent.access_id
    else:
        assert rows[child.access_id] == child

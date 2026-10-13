"""W661: a role change's Card side as a STAGE ``role_selection`` update (EMain 18:16Z).

PB sends a fixed policy delta and no Card content; the Hub applies it to the current
C or My through the ordinary reselect builder. Real planner, real C/My Cards; the catalog
resolution is W607's stand-in.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.participant_card_version import stage_authorization
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.role_selection_plan import role_update_shape_valid
from test_w607_existing_card_selection_plan import ACTOR, PROJECT, REQUEST, TARGET, _cards
from test_w607_existing_card_selection_plan import _Host as _ResolverHost

PB = "problem-board"
MINIMUM = ["project.cards.manage", "project.people.set_role"]
GRANTS = {"project.read": ["work:read"], "project.cards.manage": ["work:admin"],
          "project.people.set_role": ["work:admin"], "project.problems.edit": ["work:write"]}


def _world():
    project, control, my = _cards()
    base = {"resource_grants": {PB: ("work:read",), "other": ("x:read",)},
            "resource_operations": {PB: ("project.read",), "other": ("x.list",)}}
    return project, dataclasses.replace(control, **base), dataclasses.replace(my, **base)


class _Host(_ResolverHost):
    def __init__(self, *cards):
        super().__init__()
        self.by_id = {card.access_id: card for card in cards}

    async def _active_catalog(self):
        return SimpleNamespace(version="catalog-v2", content_hash="c" * 64)

    def _cards(self):
        by_id = self.by_id

        class _Cards:
            async def load_current(self, access_id, *, subject_hash):
                card = by_id.get(access_id)
                return None if card is None or subject_hash_for(card.grantor_subject) != subject_hash else (card, None)

        return _Cards()


def _role(card, *, add_grants=(), add_operations=(), remove_operations=(), operation_grants=None, **override):
    return {"kind": "role_selection", "target_subject": TARGET, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": 0, "resource": PB,
            "add_grants": list(add_grants), "add_operations": list(add_operations),
            "remove_operations": list(remove_operations), "operation_grants": operation_grants, **override}


async def _plan(host, updates):
    data = {"scope": PROJECT, "request_id": REQUEST, "actor_subject": ACTOR, "delegable_grants": ["work:admin"],
            "project_control": None, "creations": [], "updates": updates}
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=updates, actor_subject=ACTOR,
                                     actor_kind="caller", request_id=REQUEST,
                                     authorization=stage_authorization(data, "d" * 64))


def _after(plan):
    return {card["access_id"]: (card, CardAuthority.from_mapping(card["candidate"]))
            for card in plan["plan"]["candidate_value"]["cards"]}


@pytest.mark.asyncio
async def test_a_promotion_adds_the_fixed_minimum_to_c_and_my_and_pins_their_current_versions():
    project, control, my = _world()
    updates = [_role(card, add_grants=["work:admin"], add_operations=MINIMUM) for card in (control, my)]
    plan = await _plan(_Host(project, control, my), updates)
    assert plan["ok"] is True, plan
    after = _after(plan)
    for card in (control, my):
        member, new = after[card.access_id]
        assert member["original_revision"] == card.card_revision  # base_version: the version the Hub read
        assert set(new.resource_grants[PB]) == {"work:read", "work:admin"}
        assert set(new.resource_operations[PB]) == {"project.read", *MINIMUM}
        assert new.resource_grants["other"] == ("x:read",) and new.resource_operations["other"] == ("x.list",)


@pytest.mark.asyncio
async def test_a_demotion_removes_operations_and_recomputes_grants_from_pbs_map():
    project, control, my = _world()
    control = dataclasses.replace(control, resource_grants={**control.resource_grants, PB: ("work:read", "work:admin")},
                                  resource_operations={**control.resource_operations, PB: ("project.read", *MINIMUM)})
    plan = await _plan(_Host(project, control, my),
                       [_role(control, remove_operations=MINIMUM, operation_grants=GRANTS)])
    assert plan["ok"] is True, plan
    _, new = _after(plan)[control.access_id]
    assert new.resource_operations[PB] == ("project.read",) and new.resource_grants[PB] == ("work:read",)
    assert new.resource_grants["other"] == ("x:read",)


@pytest.mark.asyncio
async def test_a_remaining_operation_without_a_declared_grant_is_never_guessed():
    project, control, my = _world()
    plan = await _plan(_Host(project, control, my),
                       [_role(control, remove_operations=["project.cards.manage"], operation_grants={})])
    assert plan == {"ok": False, "error": "card_plan_role_operation_unknown", "status": 409}


@pytest.mark.asyncio
async def test_an_already_applied_role_is_named_so_and_an_unchanged_card_is_no_member():
    project, control, my = _world()
    # v6.3 item 6: the retry after a PUBLISH whose answer PB lost; the handler answers a signed already_applied.
    plan = await _plan(_Host(project, control, my), [_role(control, add_grants=["work:read"]),
                                                     _role(my, add_grants=["work:read"])])
    assert plan["ok"] is False and plan["error"] == "card_plan_already_applied"
    mixed = await _plan(_Host(project, control, my), [_role(control, add_grants=["work:read"]),
                                                      _role(my, add_grants=["work:admin"])])
    assert mixed["ok"] is True and set(_after(mixed)) == {my.access_id}


@pytest.mark.asyncio
async def test_a_project_control_is_not_a_role_target():
    project, control, my = _world()
    plan = await _plan(_Host(project, control, my), [_role(project, add_grants=["work:admin"])])
    assert plan["ok"] is False and plan["error"] == "card_plan_update_scope_invalid"


def test_only_the_closed_shape_is_accepted():
    _, control, _ = _world()
    good_add = _role(control, add_grants=["work:admin"])
    good_remove = _role(control, remove_operations=["a"], operation_grants={"b": ["g"]})
    # W810: a demotion may remove and add in one update, with the map that recomputes its grants.
    good_both = _role(control, remove_operations=["a"], add_operations=["b"], add_grants=["g"],
                      operation_grants={"b": ["g"]})
    assert role_update_shape_valid(good_add) and role_update_shape_valid(good_remove)
    assert role_update_shape_valid(good_both)
    for bad in ({**good_both, "operation_grants": None},  # a removal always carries PB's map
                {**good_both, "add_operations": ["a", "b"]},  # an operation both removed and added
                {**good_add, "original_revision": 3},  # PB never sends a version it would have had to read
                {**good_add, "add_grants": [], "add_operations": []},  # no direction
                {**good_add, "operation_grants": {}},  # a map only for a removal
                {**good_remove, "operation_grants": None},
                {**good_add, "selection": {}}):
        assert not role_update_shape_valid(bad)


def test_a_demotion_map_may_declare_every_pb_operation():
    _, control, _ = _world()
    every = {f"project.op{index}": ["work:read"] for index in range(94)}
    assert role_update_shape_valid(_role(control, remove_operations=["project.op1"], operation_grants=every))
    assert not role_update_shape_valid(_role(control, remove_operations=["a"],
                                             operation_grants={f"op{i}": ["g"] for i in range(257)}))


MEMBER = ["project.read", "project.problems.edit"]


@pytest.mark.asyncio
async def test_a_demotion_removes_and_adds_in_one_update_and_recomputes_grants_after_both():
    """W810: an incomplete admin (admin operations, but missing a member default) is demoted to exactly
    the member set on C and My in one save: remove the admin operations and add the missing defaults."""
    project, control, my = _world()
    incomplete = {"resource_grants": {**control.resource_grants, PB: ("work:admin",)},
                  "resource_operations": {**control.resource_operations, PB: tuple(MINIMUM)}}
    control, my = dataclasses.replace(control, **incomplete), dataclasses.replace(my, **incomplete)
    updates = [_role(card, remove_operations=MINIMUM, add_operations=MEMBER,
                     add_grants=sorted({g for op in MEMBER for g in GRANTS[op]}), operation_grants=GRANTS)
               for card in (control, my)]
    plan = await _plan(_Host(project, control, my), updates)
    assert plan["ok"] is True, plan
    after = _after(plan)
    for card in (control, my):
        _, new = after[card.access_id]
        assert set(new.resource_operations[PB]) == set(MEMBER)  # exactly the member set
        assert set(new.resource_grants[PB]) == {"work:read", "work:write"}  # work:admin recomputed away
        assert new.resource_grants["other"] == ("x:read",) and new.resource_operations["other"] == ("x.list",)


@pytest.mark.asyncio
async def test_a_combined_delta_still_never_guesses_a_grant():
    project, control, my = _world()
    plan = await _plan(_Host(project, control, my),
                       [_role(control, remove_operations=["project.cards.manage"], add_operations=["project.x"],
                              operation_grants={"project.read": ["work:read"]})])
    assert plan == {"ok": False, "error": "card_plan_role_operation_unknown", "status": 409}


@pytest.mark.asyncio
async def test_a_combined_delta_already_in_place_is_named_already_applied():
    project, control, my = _world()
    member = {"resource_grants": {**control.resource_grants, PB: ("work:read", "work:write")},
              "resource_operations": {**control.resource_operations, PB: tuple(MEMBER)}}
    control = dataclasses.replace(control, **member)
    plan = await _plan(_Host(project, control, my),
                       [_role(control, remove_operations=MINIMUM, add_operations=MEMBER,
                              add_grants=["work:read", "work:write"], operation_grants=GRANTS)])
    assert plan["ok"] is False and plan["error"] == "card_plan_already_applied"

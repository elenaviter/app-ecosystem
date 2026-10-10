"""W661: My Reset as a STAGE-owned ``reset_to_control`` update (EMain 18:07Z).

The Hub reads My and its effective Control and recomputes the display the person saw with the
same pure function; a stale display, a moved Control or a moved My is refused by name, and the
new My changes only that one service's selection.
"""

from __future__ import annotations

import dataclasses

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.participant_card_version import stage_authorization
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.managed_card_reset import managed_card_reset_display
from connection_hub.delegated_credentials.managed_card_reset_plan import (
    build_reset_to_control_update, reset_update_shape_valid,
)
from test_w607_existing_card_selection_plan import ACTOR, PROJECT, REQUEST, TARGET, _cards, _decision

SERVICE = "service-a"


def _world(with_project=False):
    project, control, my = _cards()
    control = dataclasses.replace(control, resource_grants={SERVICE: ("read", "write")},
                                  resource_operations={SERVICE: ("list", "write")})
    my = dataclasses.replace(my, resource_grants={SERVICE: ("read",), "service-b": ("read",)},
                             resource_operations={SERVICE: ("list",), "service-b": ("list",)})
    return (project, control, my) if with_project else (control, my)


class _Host:
    def __init__(self, control):
        self.control = control

    async def _compose_with_control(self, _record):
        return (None if self.control is None else record_from_card(self.control)), None


def _update(my, ctl, **override):
    _, _, digest = managed_card_reset_display(my, ctl, resource=SERVICE)
    return {"kind": "reset_to_control", "target_subject": TARGET, "access_id": my.access_id,
            "subject_hash": subject_hash_for(my.grantor_subject), "original_revision": my.card_revision,
            "resource": SERVICE, "display_digest": digest,
            "control": {"access_id": ctl.access_id, "subject_hash": subject_hash_for(ctl.grantor_subject)},
            **override}


async def _build(host, my, update):
    return await build_reset_to_control_update(host, original=my, update=update, decision=_decision(),
                                               project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST)


@pytest.mark.asyncio
async def test_a_reset_takes_only_that_services_selection_from_the_control():
    control, my = _world()
    member = (await _build(_Host(control), my, _update(my, control)))["member"]
    after = CardAuthority.from_mapping(member["candidate"])
    assert member["action"] == "update" and member["original_revision"] == my.card_revision
    assert after.card_revision == my.card_revision + 1
    assert after.resource_grants[SERVICE] == ("read", "write")
    assert after.resource_operations[SERVICE] == ("list", "write")
    assert after.resource_grants["service-b"] == ("read",)  # every other service unchanged
    assert dataclasses.replace(after, card_revision=my.card_revision, resource_grants=my.resource_grants,
                               resource_operations=my.resource_operations,
                               named_service_operations=my.named_service_operations) == my


@pytest.mark.asyncio
async def test_a_display_the_control_has_since_changed_is_refused_as_moved():
    control, my = _world()
    update = _update(my, control)
    changed = dataclasses.replace(control, resource_operations={SERVICE: ("list",)},
                                  resource_grants={SERVICE: ("read", "admin")})
    with pytest.raises(DecisionRefused, match="card_plan_reset_display_moved"):
        await _build(_Host(changed), my, update)


@pytest.mark.asyncio
@pytest.mark.parametrize("override,code", [
    ({"display_digest": "0" * 64}, "card_plan_reset_display_moved"),
    ({"control": {"access_id": "another-control", "subject_hash": "h"}}, "card_plan_reset_control_moved"),
])
async def test_a_stale_digest_or_another_control_is_refused(override, code):
    control, my = _world()
    with pytest.raises(DecisionRefused, match=code):
        await _build(_Host(control), my, _update(my, control, **override))


@pytest.mark.asyncio
async def test_an_unresolvable_control_unchanged_or_unheld_service_is_refused():
    control, my = _world()
    with pytest.raises(DecisionRefused, match="card_plan_reset_control_moved"):
        await _build(_Host(None), my, _update(my, control))
    same = dataclasses.replace(control, resource_grants={SERVICE: ("read",)}, resource_operations={SERVICE: ("list",)})
    with pytest.raises(Exception):  # nothing would change: the display itself refuses
        _update(my, same)
    with pytest.raises(DecisionRefused, match="card_plan_reset_unchanged"):
        await _build(_Host(same), my, _update(my, control))


@pytest.mark.asyncio
async def test_a_person_control_is_not_a_reset_target():
    control, my = _world()
    with pytest.raises(DecisionRefused, match="card_plan_update_scope_invalid"):
        await _build(_Host(control), control, _update(my, control))


def test_only_the_closed_shape_is_accepted():
    control, my = _world()
    update = _update(my, control)
    assert reset_update_shape_valid(update)
    for broken in ({**update, "selection": {}}, {**update, "display_digest": "XYZ"},
                   {**update, "control": {"access_id": "c"}}, {k: v for k, v in update.items() if k != "resource"}):
        assert not reset_update_shape_valid(broken)


class _PlannerHost(_Host):
    """Just enough of the Hub host for plan_card_lifecycle's update path."""

    def __init__(self, control, my, project):
        super().__init__(control)
        self.my, self.by_id = my, {card.access_id: card for card in (my, control, project)}

    async def _active_catalog(self):
        from types import SimpleNamespace
        return SimpleNamespace(version="catalog-v1", content_hash="c" * 64)

    @staticmethod
    def _version_of(active):
        return active.version

    def _cards(self):
        by_id = self.by_id

        class _Cards:
            async def load_current(self, access_id, *, subject_hash):
                card = by_id.get(access_id)
                return None if card is None or subject_hash_for(card.grantor_subject) != subject_hash else (card, None)

        return _Cards()


@pytest.mark.asyncio
async def test_the_planner_runs_a_reset_update_under_pbs_role_decision_and_refuses_extra_keys():
    project, control, my = _world(with_project=True)
    update = _update(my, control)
    data = {"scope": PROJECT, "request_id": REQUEST, "actor_subject": ACTOR, "delegable_grants": [],
            "project_control": None, "creations": [], "updates": [update]}
    plan = await plan_card_lifecycle(_PlannerHost(control, my, project), project_ref=PROJECT, creations=[], updates=[update],
                                     actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST,
                                     authorization=stage_authorization(data, "d" * 64))
    assert plan["ok"] is True, plan
    [card] = plan["plan"]["candidate_value"]["cards"]
    assert card["action"] == "update" and card["access_id"] == my.access_id
    bad = {**update, "selection": {"resource_grants": {}}}
    refused = await plan_card_lifecycle(_PlannerHost(control, my, project), project_ref=PROJECT, creations=[], updates=[bad],
                                        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST,
                                        authorization=stage_authorization({**data, "updates": [bad]}, "d" * 64))
    assert refused["ok"] is False and refused["error"] == "card_plan_update_invalid"

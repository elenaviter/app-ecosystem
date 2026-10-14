"""W850: a project agent Card that follows its role takes the role's new operations as an add-only
``role_selection`` (operator, 2026-10-14: "A. Only agents that join a project from now on.").

PB names the agent Card itself as the target (target_subject == access_id); the step is authorized as
that agent Card's update, and the Hub applies the add-only delta to the Card it reads now through the
managed agent-Card builder. Real planner, real Cards; the catalog resolution is W607's stand-in.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import plan_steps
from connection_hub.delegated_credentials.cards.model import CardAuthority, ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_AGENT_CARD_UPDATE, PROJECT_PERSON_CONTROL_UPDATE,
)
from test_w661_role_selection_plan import PB, _Host, _after, _plan, _role, _world

OWNER = "synthetic-agent-owner"
WORKER_NEW = ["work.claim"]


def _agent(project, **override):
    base = dataclasses.replace(
        project, access_id="agent-card-w850", grantor_subject=OWNER, delegate_subject="integration:agent-w850",
        issuer_kind="", issuer_ref="", source="agent", card_kind="agent", card_revision=7,
        resource_grants={PB: ("work:read",), "other": ("x:read",)},
        resource_operations={PB: ("project.read",), "other": ("x.list",)},
        control_card=ControlCardBinding(control_id=project.access_id, issuer_kind="application",
                                        issuer_ref=project.issuer_ref, control_revision=project.card_revision,
                                        holder_subject=project.grantor_subject))
    return dataclasses.replace(base, **override)


def _follow(card, **override):
    return _role(card, target_subject=card.access_id, **override)


def test_a_role_selection_naming_the_card_is_authorized_as_that_agent_cards_update():
    project, control, _ = _world()
    agent = _agent(project)
    steps = plan_steps([], [_follow(agent, add_operations=WORKER_NEW), _role(control, add_grants=["work:admin"])])
    assert [(step.operation, step.target_subject) for step in steps] == [
        (PROJECT_AGENT_CARD_UPDATE, agent.access_id), (PROJECT_PERSON_CONTROL_UPDATE, "person-1")]


@pytest.mark.asyncio
async def test_a_following_agent_card_gains_the_new_role_operation_and_keeps_everything_else():
    project, control, my = _world()
    agent = _agent(project)
    plan = await _plan(_Host(project, control, my, agent),
                       [_follow(agent, add_grants=["work:write"], add_operations=WORKER_NEW)])
    assert plan["ok"] is True, plan
    member, new = _after(plan)[agent.access_id]
    assert member["original_revision"] == agent.card_revision  # base_version: the version the Hub read now
    assert set(new.resource_operations[PB]) == {"project.read", *WORKER_NEW}
    assert set(new.resource_grants[PB]) == {"work:read", "work:write"}
    assert new.resource_grants["other"] == ("x:read",) and new.resource_operations["other"] == ("x.list",)
    assert new.control_card == agent.control_card and new.card_revision == agent.card_revision + 1


@pytest.mark.asyncio
async def test_an_admins_earlier_removal_is_not_re_added_because_pb_sends_only_new_role_operations():
    project, control, my = _world()
    # The admin removed project.read earlier; PB's follow names only the role's new operation.
    agent = _agent(project, resource_operations={PB: (), "other": ("x.list",)})
    plan = await _plan(_Host(project, control, my, agent), [_follow(agent, add_operations=WORKER_NEW)])
    assert plan["ok"] is True, plan
    _, new = _after(plan)[agent.access_id]
    assert set(new.resource_operations[PB]) == set(WORKER_NEW)


@pytest.mark.asyncio
async def test_a_card_that_already_holds_the_role_operations_stays_a_read():
    project, control, my = _world()
    agent = _agent(project, resource_operations={PB: ("project.read", *WORKER_NEW), "other": ("x.list",)})
    plan = await _plan(_Host(project, control, my, agent), [_follow(agent, add_operations=WORKER_NEW)])
    assert plan["ok"] is False and plan["error"] == "card_plan_already_applied"


@pytest.mark.asyncio
async def test_a_follow_never_removes():
    project, control, my = _world()
    agent = _agent(project)
    plan = await _plan(_Host(project, control, my, agent),
                       [_follow(agent, remove_operations=["project.read"], operation_grants={"x": ["work:read"]})])
    assert plan["ok"] is False and plan["error"] == "card_plan_agent_follow_add_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_project", "unbound", "revoked", "other_resource", "person_card"])
async def test_only_an_active_agent_card_bound_under_this_projects_control_and_its_own_resource(case):
    project, control, my = _world()
    agent = _agent(project)
    if case == "other_project":
        agent = dataclasses.replace(agent, control_card=dataclasses.replace(
            agent.control_card, control_id="someone-elses-control", issuer_ref="work:project:other"))
    elif case == "unbound":
        agent = dataclasses.replace(agent, control_card=None)
    elif case == "revoked":
        agent = dataclasses.replace(agent, state="revoked")
    update = _follow(agent, add_operations=WORKER_NEW)
    if case == "other_resource":
        update = {**update, "resource": "a-resource-the-card-does-not-hold"}
    if case == "person_card":
        # A person's C named as its own target is not an agent Card.
        update = _follow(control, add_operations=WORKER_NEW)
    host = _Host(project, control, my, agent)
    plan = await _plan(host, [update])
    assert plan["ok"] is False and plan["error"] == "card_plan_update_scope_invalid", plan
    assert host.writes == 0


@pytest.mark.asyncio
async def test_the_person_role_selection_path_is_unchanged_beside_an_agent_follow():
    project, control, my = _world()
    agent = _agent(project)
    plan = await _plan(_Host(project, control, my, agent),
                       [_role(control, add_grants=["work:admin"], add_operations=["project.cards.manage"]),
                        _follow(agent, add_operations=WORKER_NEW)])
    assert plan["ok"] is True, plan
    after = _after(plan)
    assert set(after[control.access_id][1].resource_operations[PB]) == {"project.read", "project.cards.manage"}
    assert set(after[agent.access_id][1].resource_operations[PB]) == {"project.read", *WORKER_NEW}
    assert subject_hash_for(after[agent.access_id][1].grantor_subject) == subject_hash_for(OWNER)
    assert isinstance(after[agent.access_id][1], CardAuthority)

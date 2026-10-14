"""W852: ``reselect_agent_card`` takes only an agent's own Card, whatever binding row PB sends.

Found by op-assist building W850 and confirmed by Mint (2026-10-14): ``managed_target_kind(kind="agent_card")``
checked only the binding under the project Control, so a person's C (bound under P) or a My Card given a forged
binding passed. The agent-own-Card check (W639's, which W850's role path had beside it) now sits inside
``managed_target_kind``, so every agent_card path refuses a person's Card. It reads the Card the plan already
loaded: no extra Hub read (operator on W844: "make sure that W844 will . be implemented in the efficient way and
wont make the roudntrip waiting and slow").
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.managed_card_selection_plan import managed_target_kind
from service_foundation.coordination.durable_decision_log import DecisionRefused
from test_w661_role_selection_plan import PB, _Host, _plan, _world
from test_w850_agent_role_follow_plan import _agent

REFUSAL = "card_plan_agent_card_target_not_agent"


class _CountingHost(_Host):
    """Counts the Hub's Card reads; the resolver stand-in already records resolutions and writes."""

    def __init__(self, *cards):
        super().__init__(*cards)
        self.reads = []

    def _cards(self):
        inner, reads = super()._cards(), self.reads

        class _Cards:
            async def load_current(self, access_id, *, subject_hash):
                reads.append(access_id)
                return await inner.load_current(access_id, subject_hash=subject_hash)

        return _Cards()


def _bound_under_p(card, project):
    """The binding a wrong or forged PB row would claim: directly under the project's Control."""
    return dataclasses.replace(card, control_card=_agent(project).control_card)


def _reselect(card):
    return {"kind": "reselect_agent_card", "target_subject": card.access_id, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision,
            "selection": {"resource_operations": {PB: ["project.read", "work.claim"]}}}


@pytest.mark.parametrize("which", ["person_control", "my_card"])
def test_the_target_check_refuses_a_persons_card_bound_under_p_by_name(which):
    project, control, my = _world()
    card = _bound_under_p(control if which == "person_control" else my, project)
    with pytest.raises(DecisionRefused, match=REFUSAL):
        managed_target_kind(card, project_ref=project.issuer_ref, kind="agent_card")


@pytest.mark.parametrize("change", [{"card_kind": "control"}, {"delegate_subject": ""},
                                    {"delegate_subject": "person:synthetic"}, {"source": "manual"},
                                    {"delegate_subject": "integration:self", "grantor_subject": "integration:self"}])
def test_each_part_of_the_agent_own_card_check_holds(change):
    project, _, _ = _world()
    assert managed_target_kind(_agent(project), project_ref=project.issuer_ref, kind="agent_card") == "agent_card"
    with pytest.raises(DecisionRefused, match=REFUSAL):
        managed_target_kind(dataclasses.replace(_agent(project), **change), project_ref=project.issuer_ref,
                            kind="agent_card")


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["person_control", "my_card"])
async def test_a_reselect_agent_card_naming_a_persons_card_is_refused_with_one_read_and_no_write(which):
    project, control, my = _world()
    card = _bound_under_p(control if which == "person_control" else my, project)
    host = _CountingHost(project, card)
    plan = await _plan(host, [_reselect(card)])
    assert plan["ok"] is False and plan["error"] == REFUSAL, plan
    assert host.reads == [card.access_id], "the check reads only the Card the plan already loads"
    assert host.calls == [] and host.writes == 0


@pytest.mark.asyncio
async def test_an_agents_own_card_still_plans_with_the_same_single_read():
    project, _, _ = _world()
    agent = _agent(project)
    host = _CountingHost(project, agent)
    plan = await _plan(host, [_reselect(agent)])
    assert plan["ok"] is True, plan
    # The agent Card and its project Control, each once: the reads main already made, none added.
    assert host.reads == [agent.access_id, project.access_id] and len(host.calls) == 1 and host.writes == 0

"""A same-owner agent link (holder_subject left empty) passes the agent save and lifecycle checks (10 Oct 2026).

attach_control_card records holder_subject only when P's holder is not the Card's own grantor; the resolver
reads ``holder_subject or grantor_subject``. The agent save target check (managed_card_selection_plan) and the
agent lifecycle check (agent_lifecycle_plan) read the RAW holder, so an agent owned by P's own holder - the
operator's agents under her own project - was refused although its link is exactly right (codex-infra's
corrected probe: 18 of 20 roster agents). Both checks now use the resolved holder, as the resolver does.
"""
from __future__ import annotations

import dataclasses
import time

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials import agent_lifecycle_plan
from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.managed_card_selection_plan import managed_target_kind
from connection_hub.delegated_credentials.project_authorization import ProjectControlLocator

from test_p0_legacy_binding_repair import _dsn, _live_card_transactions
from test_project_person_access import PROJECT_REF
from test_w502_person_control_binding import CREATOR, _project_control, _service
from test_w580_bound_card_writers import redis_client  # noqa: F401 - fixture

P_ID = control_card_id_for_issuer("application", PROJECT_REF, grantor_subject=CREATOR)


def _agent_of(owner, *, holder):
    from test_project_person_control import _authority
    return dataclasses.replace(_authority(), access_id="agent-card-same-owner", grantor_subject=owner,
        issuer_kind="", issuer_ref="", issuer_label="", source="agent", card_kind="agent", card_revision=1,
        properties={}, control_card=ControlCardBinding(control_id=P_ID, issuer_ref=PROJECT_REF,
            issuer_kind="application", control_revision=1, holder_subject=holder))


@pytest.mark.parametrize("owner, holder", [(CREATOR, ""), ("synthetic-other-owner", CREATOR)])
def test_the_agent_save_target_check_reads_the_resolved_holder(owner, holder):
    """Same owner (holder empty, as attach writes it) and another owner (holder recorded) both pass."""
    assert managed_target_kind(_agent_of(owner, holder=holder), project_ref=PROJECT_REF, kind="agent_card") == "agent_card"


def test_a_same_owner_agent_linked_to_another_p_is_still_refused():
    agent = dataclasses.replace(_agent_of(CREATOR, holder=""), control_card=dataclasses.replace(
        _agent_of(CREATOR, holder="").control_card, control_id="synthetic-other-p"))
    with pytest.raises(DecisionRefused, match="card_plan_update_scope_invalid"):
        managed_target_kind(agent, project_ref=PROJECT_REF, kind="agent_card")


@pytest.mark.parametrize("owner, holder", [(CREATOR, ""), ("synthetic-other-owner", CREATOR)])
def test_the_agent_lifecycle_check_reads_the_resolved_holder(owner, holder):
    decision = type("Decision", (), {"project_control": ProjectControlLocator(control_id=P_ID, holder_subject=CREATOR)})()
    binding = agent_lifecycle_plan._existing_binding(_agent_of(owner, holder=holder), project_ref=PROJECT_REF,
                                                     decision=decision)
    assert binding.control_id == P_ID


def test_the_lifecycle_check_still_refuses_another_holder():
    decision = type("Decision", (), {"project_control": ProjectControlLocator(control_id=P_ID,
                                                                             holder_subject="synthetic-other")})()
    with pytest.raises(DecisionRefused, match="agent_plan_project_control_invalid"):
        agent_lifecycle_plan._existing_binding(_agent_of(CREATOR, holder=""), project_ref=PROJECT_REF,
                                               decision=decision)


@pytest.mark.asyncio
async def test_a_same_owner_agent_save_plans_with_card_transactions_on(tmp_path, redis_client):  # noqa: F811
    """End to end on the Hub (live configuration): the operator's own agent, linked under her own P."""
    from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
    from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
    from connection_hub.delegated_credentials.project_authorization import (
        LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep, ProjectAuthorizationDecision,
    )
    from test_w502_my_card_fence_real_path import ADMIN, GRANT
    from test_w580_bound_card_writers import _memories

    if not _dsn():
        pytest.skip("needs CONNECTION_HUB_TEST_POSTGRES_DSN(_FILE): a disposable PostgreSQL")
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    p = (await h.store.read_current_authority(subject_hash=subject_hash_for(CREATOR), access_id=p_id))[1]
    agent = dataclasses.replace(p, access_id="agent-card-same-owner", grantor_subject=CREATOR, issuer_kind="",
        issuer_ref="", issuer_label="", source="agent", card_kind="agent", card_revision=1, properties={},
        control_card=ControlCardBinding(control_id=p_id, issuer_ref=PROJECT_REF, issuer_kind="application",
                                        control_revision=p.card_revision, holder_subject=""))
    await h.cards.commit(agent, subject_hash=subject_hash_for(CREATOR), expected_revision=0, now=int(time.time()))
    pool = await _live_card_transactions(h)
    try:
        updates = [{"kind": "reselect_agent_card", "target_subject": agent.access_id, "access_id": agent.access_id,
                    "subject_hash": subject_hash_for(CREATOR), "original_revision": agent.card_revision,
                    "selection": {"resource_grants": {_memories(): [GRANT]}, "resource_operations": {_memories(): []}}}]
        steps = (LifecyclePlanStep(ref="update:0", operation=UPDATE_STEPS["reselect_agent_card"],
                                   target_subject=agent.access_id),)
        request = LifecyclePlanAuthorizationRequest(actor_subject=ADMIN, project_ref=PROJECT_REF,
                                                    request_id="same-owner-save", request_digest="a" * 64, steps=steps)
        authorization = LifecyclePlanAuthorization(request=request, decisions=tuple(
            (step.ref, ProjectAuthorizationDecision.allow(request.step_request(step), delegable_grants=(GRANT,),
                                                          platform_admin=True)) for step in steps))
        planned = await plan_card_lifecycle(h.service, project_ref=PROJECT_REF, creations=[], updates=updates,
                                            actor_subject=ADMIN, actor_kind="caller", request_id="same-owner-save",
                                            authorization=authorization)
        assert planned["ok"] is True, planned
        # ... and COMMITS: staged as the participant stages it, decided committed.
        from datetime import datetime, timezone
        from connection_hub.delegated_credentials.cards import transaction_store as tx
        from connection_hub.delegated_credentials.cards.model import CardAuthority
        from test_card_transaction_store import Decisions
        plan = planned["plan"]
        members = [(m["subject_hash"], agent, CardAuthority.from_mapping(m["candidate"]), m["action"])
                   for m in plan["candidate_value"]["cards"]]
        decisions = Decisions()
        tx.bind_transaction_decisions(h.store, decisions)

        class _Reservations:
            async def reserve(self, **kwargs):
                pass

            async def release(self, transaction_id, *, intent_digest):
                pass

        tx.bind_catalog_reservations(h.store, _Reservations())
        group_id, intent, now = "c3" * 32, "d4" * 32, int(time.time())
        staged = await h.cards.stage_group_transaction(
            transaction_id=group_id, intent_digest=intent, participant="project", members=members,
            now=datetime.fromtimestamp(now, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
        assert staged["staged"] is True, staged
        decisions.recorded[group_id] = "committed"
        decided = await h.cards.decide_group_transaction(transaction_id=group_id, intent_digest=intent,
                                                         decision="committed", now=now)
        assert decided["state"] == "committed", decided
        saved = (await h.store.read_current_authority(subject_hash=subject_hash_for(CREATOR),
                                                      access_id=agent.access_id))[1]
        assert saved.card_revision == agent.card_revision + 1 and not saved.resource_operations.get(_memories())
        assert saved.control_card == agent.control_card, "the same-owner link is kept as it was"
    finally:
        await pool.close()

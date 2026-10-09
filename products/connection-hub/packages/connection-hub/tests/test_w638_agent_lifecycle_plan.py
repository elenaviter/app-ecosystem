"""Agent PLAN candidates must be tied to the exact Card, project and step."""
import dataclasses

import pytest

from connection_hub.delegated_credentials.agent_lifecycle_plan import (
    AGENT_ATTACH_OPERATION, AGENT_DETACH_OPERATION, AGENT_PROFILE_OPERATION,
    APPLY_AGENT_PROFILE, ATTACH_AGENT, DETACH_AGENT,
    build_agent_lifecycle_update,
)
from connection_hub.delegated_credentials.automation_access import card_authority_from_record
from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority, ControlCardBinding, NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_PROPERTY, CONTROL_SNAPSHOT_SCHEMA,
)
from connection_hub.delegated_credentials.project_authorization import (
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep,
    ProjectAuthorizationDecision, ProjectControlLocator,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused
from test_card_profile_lever import DECLARED_RESOURCE, _worker_card

PROJECT = "work:project:synthetic"
OWNER = "synthetic-owner"
AGENT = "integration:synthetic-worker"
ACTOR = "synthetic-admin"
REQUEST = "synthetic-request"


def _parent():
    return CardAuthority(access_id=control_card_id_for_issuer(
        "application", PROJECT, grantor_subject=OWNER), client_id="synthetic-app",
        grantor_subject=OWNER, delegate_subject="", source="control",
        card_kind="control", card_revision=1, issuer_kind="application", issuer_ref=PROJECT,
        catalog_version="synthetic-catalog", composition_mode="and",
        named_service_operations=NamedServiceSelection.none(),
        properties={CONTROL_SNAPSHOT_PROPERTY: {"schema": CONTROL_SNAPSHOT_SCHEMA,
            "mode": "exact", "state": "exact", "basis_catalog_version": "synthetic-catalog"}})


def _agent():
    return CardAuthority(access_id="synthetic-agent-card", client_id="synthetic-client",
        grantor_subject=OWNER, delegate_subject=AGENT, source="oauth", card_kind="automation",
        card_revision=3)


def _decision(kind, parent):
    return ProjectAuthorizationDecision(allowed=True, actor_subject=ACTOR,
        project_ref=PROJECT, target_subject=AGENT,
        operation=AGENT_ATTACH_OPERATION if kind == ATTACH_AGENT else AGENT_DETACH_OPERATION,
        request_id=REQUEST, project_control=ProjectControlLocator(
            parent.access_id, parent.grantor_subject))


def _update(kind, agent):
    value = {"kind": kind, "target_subject": AGENT, "access_id": agent.access_id,
        "subject_hash": subject_hash_for(OWNER), "original_revision": agent.card_revision}
    if kind == ATTACH_AGENT:
        value["parent"] = {"access_id": _parent().access_id, "holder_subject": OWNER}
    return value


class _Cards:
    def __init__(self, *cards):
        self.current = {(subject_hash_for(card.grantor_subject), card.access_id): card for card in cards}

    async def load_current(self, access_id, *, subject_hash):
        card = self.current.get((subject_hash, access_id))
        return (card, object()) if card is not None else None


class _Host:
    def __init__(self, *cards):
        self.cards = _Cards(*cards)
        self.catalog = CatalogDocument.build({"delegated_credentials": {"oauth": {"resources": []}}})

    def _cards(self):
        return self.cards

    async def _active_catalog(self):
        return self.catalog

    def _version_of(self, active):
        return active.version


def _authorization(kind, parent):
    step = LifecyclePlanStep(ref="update:0", operation=_decision(kind, parent).operation,
                             target_subject=AGENT)
    request = LifecyclePlanAuthorizationRequest(actor_subject=ACTOR, project_ref=PROJECT,
        request_id=REQUEST, request_digest="a" * 64, steps=(step,))
    return LifecyclePlanAuthorization(request=request, decisions=((step.ref,
        ProjectAuthorizationDecision.allow(request.step_request(step),
            project_control=ProjectControlLocator(parent.access_id, parent.grantor_subject))),))


@pytest.mark.asyncio
async def test_attach_then_detach_builds_exact_next_revisions_without_writes():
    parent, agent = _parent(), _agent()
    attached = await build_agent_lifecycle_update(None, original=agent,
        update=_update(ATTACH_AGENT, agent), active=None, decision=_decision(ATTACH_AGENT, parent),
        project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST, now=1,
        parent=parent)
    member = attached["member"]
    assert member["action"] == "attach"
    assert member["original_revision"] == 3
    assert member["candidate"]["card_revision"] == 4
    assert member["candidate"]["control_card"]["control_id"] == parent.access_id
    assert agent.control_card is None

    attached_card = CardAuthority.from_mapping(member["candidate"])
    detached = await build_agent_lifecycle_update(None, original=attached_card,
        update=_update(DETACH_AGENT, attached_card), active=None,
        decision=_decision(DETACH_AGENT, parent), project_ref=PROJECT,
        actor_subject=ACTOR, request_id=REQUEST, now=2)
    assert detached["member"]["action"] == "detach"
    assert detached["member"]["candidate"]["card_revision"] == 5
    assert detached["member"]["candidate"].get("control_card") is None
    assert attached_card.control_card is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ATTACH_AGENT, DETACH_AGENT])
async def test_generic_plan_holds_parent_read_and_group_accepts_attach_or_detach(kind):
    parent, agent = _parent(), _agent()
    if kind == DETACH_AGENT:
        attached = await build_agent_lifecycle_update(None, original=agent,
            update=_update(ATTACH_AGENT, agent), active=None, decision=_decision(ATTACH_AGENT, parent),
            project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST, now=1, parent=parent)
        agent = CardAuthority.from_mapping(attached["member"]["candidate"])
    host = _Host(parent, agent)
    result = await plan_card_lifecycle(host, project_ref=PROJECT, creations=[],
        updates=[_update(kind, agent)], actor_subject=ACTOR, actor_kind="caller",
        request_id=REQUEST, authorization=_authorization(kind, parent))
    assert result["ok"] is True, result
    plan = result["plan"]
    assert [(member["access_id"], member["action"]) for member in plan["candidate_value"]["cards"]] == [
        (agent.access_id, "attach" if kind == ATTACH_AGENT else "detach")]
    if kind == ATTACH_AGENT:
        assert (parent.access_id, parent.card_revision) in {
            (read["access_id"], read["revision"]) for read in plan["reads"]}
    validate_group_candidate(plan["candidate_value"], reads=plan["reads"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor", "target", "revision", "locator", "other_project"])
async def test_stale_or_foreign_agent_step_cannot_construct_candidate(change):
    parent, agent = _parent(), _agent()
    update = _update(ATTACH_AGENT, agent)
    decision = _decision(ATTACH_AGENT, parent)
    if change == "actor":
        decision = dataclasses.replace(decision, actor_subject="other-admin")
    elif change == "target":
        update["target_subject"] = "integration:other-worker"
    elif change == "revision":
        update["original_revision"] += 1
    elif change == "locator":
        decision = dataclasses.replace(decision, project_control=ProjectControlLocator("other-control", OWNER))
    else:
        parent = dataclasses.replace(parent, issuer_ref="work:project:other")
    with pytest.raises(DecisionRefused):
        await build_agent_lifecycle_update(None, original=agent, update=update, active=None,
            decision=decision, project_ref=PROJECT, actor_subject=ACTOR,
            request_id=REQUEST, now=1, parent=parent)


@pytest.mark.asyncio
async def test_profile_uses_actual_catalog_worker_and_coordinator_selections():
    service, _persistence, record = await _worker_card()
    parent = _parent()
    original = dataclasses.replace(card_authority_from_record(record),
        delegate_subject=AGENT, card_kind="agent", control_card=ControlCardBinding(
            control_id=parent.access_id, issuer_kind="application", issuer_ref=PROJECT,
            control_revision=parent.card_revision, holder_subject=OWNER))
    decision = ProjectAuthorizationDecision(allowed=True, actor_subject=ACTOR,
        project_ref=PROJECT, target_subject=AGENT, operation=AGENT_PROFILE_OPERATION,
        request_id=REQUEST, project_control=ProjectControlLocator(parent.access_id, OWNER),
        delegable_grants=("work:observe", "work:relay", "work:coordinate"))
    active = await service._active_catalog()
    for profile, included in (("coordinator", True), ("worker", False)):
        update = {"kind": APPLY_AGENT_PROFILE, "target_subject": AGENT,
            "access_id": original.access_id, "subject_hash": subject_hash_for(original.grantor_subject),
            "original_revision": original.card_revision,
            "profile": profile, "resource": DECLARED_RESOURCE}
        built = await build_agent_lifecycle_update(service, original=original,
            update=update, active=active, decision=decision, project_ref=PROJECT,
            actor_subject=ACTOR, request_id=REQUEST, now=1_800_000_000)
        candidate = CardAuthority.from_mapping(built["member"]["candidate"])
        assert ("assignment.assign" in candidate.resource_operations[DECLARED_RESOURCE]) is included
        assert candidate.access_id == original.access_id
        assert candidate.card_revision == original.card_revision + 1
        assert candidate.control_card == original.control_card
    assert original.resource_operations[DECLARED_RESOURCE] == ("project.plan.item",)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ATTACH_AGENT, DETACH_AGENT])
async def test_revision_zero_plans_against_the_current_agent_revision_and_reports_it(kind):
    # W639: the project host knows the agent Card's id, not its revision.
    parent, agent = _parent(), _agent()
    if kind == DETACH_AGENT:
        attached = await build_agent_lifecycle_update(None, original=agent,
            update=_update(ATTACH_AGENT, agent), active=None, decision=_decision(ATTACH_AGENT, parent),
            project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST, now=1, parent=parent)
        agent = CardAuthority.from_mapping(attached["member"]["candidate"])
    update = {**_update(kind, agent), "original_revision": 0}
    result = await plan_card_lifecycle(_Host(parent, agent), project_ref=PROJECT, creations=[], updates=[update],
        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST, authorization=_authorization(kind, parent))
    assert result["ok"] is True, result
    (member,) = result["plan"]["candidate_value"]["cards"]
    assert member["original_revision"] == agent.card_revision  # the exact revision PREPARE will fence


@pytest.mark.asyncio
async def test_revision_zero_is_refused_for_any_other_update_kind():
    parent = _parent()
    result = await plan_card_lifecycle(_Host(parent), project_ref=PROJECT, creations=[],
        updates=[{"kind": "revoke", "target_subject": AGENT, "access_id": parent.access_id,
                  "subject_hash": subject_hash_for(OWNER), "original_revision": 0}],
        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST, authorization=_authorization(ATTACH_AGENT, parent))
    assert result["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ATTACH_AGENT, DETACH_AGENT])
async def test_a_decision_for_another_agent_cannot_change_this_agents_card(kind):
    # The only check binding the project's authorization to the Card being changed.
    parent, agent = _parent(), _agent()
    if kind == DETACH_AGENT:
        attached = await build_agent_lifecycle_update(None, original=agent,
            update=_update(ATTACH_AGENT, agent), active=None, decision=_decision(ATTACH_AGENT, parent),
            project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST, now=1, parent=parent)
        agent = CardAuthority.from_mapping(attached["member"]["candidate"])
    decision = dataclasses.replace(_decision(kind, parent), target_subject="integration:other-worker")
    with pytest.raises(DecisionRefused, match="agent_plan_authorization_invalid"):
        await build_agent_lifecycle_update(None, original=agent, update=_update(kind, agent), active=None,
            decision=decision, project_ref=PROJECT, actor_subject=ACTOR, request_id=REQUEST, now=1,
            parent=parent)


@pytest.mark.asyncio
async def test_a_card_delegated_to_its_own_grantor_is_not_an_agent_target():
    parent = _parent()
    own = dataclasses.replace(_agent(), grantor_subject=AGENT)  # delegate == grantor: a subject's own Card
    update = {**_update(ATTACH_AGENT, own), "subject_hash": subject_hash_for(AGENT)}
    with pytest.raises(DecisionRefused, match="agent_plan_target_invalid"):
        await build_agent_lifecycle_update(None, original=own, update=update, active=None,
            decision=_decision(ATTACH_AGENT, parent), project_ref=PROJECT, actor_subject=ACTOR,
            request_id=REQUEST, now=1, parent=parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["profile", "resource"])
async def test_profile_keys_on_a_non_profile_agent_step_are_refused_by_the_planner(key):
    parent, agent = _parent(), _agent()
    update = {**_update(ATTACH_AGENT, agent), key: "worker" if key == "profile" else DECLARED_RESOURCE}
    result = await plan_card_lifecycle(_Host(parent, agent), project_ref=PROJECT, creations=[], updates=[update],
        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST, authorization=_authorization(ATTACH_AGENT, parent))
    assert result == {"ok": False, "error": "card_plan_update_invalid", "status": 400}, result

"""Probe: revision 0 is refused for a non-agent kind BY the revision rule, with a matching authorization."""
import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.project_authorization import (
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep,
    ProjectAuthorizationDecision, ProjectControlLocator,
)
from test_w638_agent_lifecycle_plan import ACTOR, AGENT, OWNER, PROJECT, REQUEST, _Host, _parent


def _authorization_for(kind, parent):
    step = LifecyclePlanStep(ref="update:0", operation=UPDATE_STEPS[kind], target_subject=AGENT)
    request = LifecyclePlanAuthorizationRequest(actor_subject=ACTOR, project_ref=PROJECT,
        request_id=REQUEST, request_digest="a" * 64, steps=(step,))
    return LifecyclePlanAuthorization(request=request, decisions=((step.ref,
        ProjectAuthorizationDecision.allow(request.step_request(step),
            project_control=ProjectControlLocator(parent.access_id, parent.grantor_subject))),))


@pytest.mark.asyncio
# W661 P4 (ec757c57): remove_person plans against the Card the Hub reads now, so 0 is valid for it; a removal
# aimed at an agent's Card is then refused by scope (below), not by the revision rule.
@pytest.mark.parametrize("kind", ["revoke", "attach"])
async def test_revision_zero_refused_by_revision_rule_for_non_agent_kind(kind):
    parent = _parent()
    update = {"kind": kind, "target_subject": AGENT, "access_id": parent.access_id,
              "subject_hash": subject_hash_for(OWNER), "original_revision": 0}
    if kind == "attach":
        update["parent"] = {"access_id": parent.access_id, "holder_subject": OWNER}
    result = await plan_card_lifecycle(_Host(parent), project_ref=PROJECT, creations=[], updates=[update],
        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST,
        authorization=_authorization_for(kind, parent))
    assert result == {"ok": False, "error": "card_plan_revision_invalid", "status": 400}, result


@pytest.mark.asyncio
async def test_revision_zero_is_valid_for_remove_person_and_its_target_is_then_checked():
    parent = _parent()
    update = {"kind": "remove_person", "target_subject": AGENT, "access_id": parent.access_id,
              "subject_hash": subject_hash_for(OWNER), "original_revision": 0}
    result = await plan_card_lifecycle(_Host(parent), project_ref=PROJECT, creations=[], updates=[update],
        actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST,
        authorization=_authorization_for("remove_person", parent))
    assert result == {"ok": False, "error": "card_plan_update_scope_invalid", "status": 403}, result

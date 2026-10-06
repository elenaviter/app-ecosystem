"""W578: one request-bound authorization envelope for a whole lifecycle plan (CodeApp 23:48).

Each step keeps its own exact host decision (one target, one operation, the
same actor, project and request); the envelope covers every requested step
exactly once and nothing else, and it never becomes a union of grants.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_CONTROL_CREATE, PROJECT_PERSON_CONTROL_CREATE, PROJECT_PERSON_CONTROL_REVOKE,
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep, ProjectAuthorizationDecision,
    ProjectAuthorizationError,
)

ACTOR, PROJECT, REQUEST, DIGEST = "platform-user-1", "work:project:quickstart", "plan-1", "d" * 64
STEPS = (LifecyclePlanStep("p", PROJECT_CONTROL_CREATE, ACTOR),
         LifecyclePlanStep("c", PROJECT_PERSON_CONTROL_CREATE, ACTOR),
         LifecyclePlanStep("claim", PROJECT_PERSON_CONTROL_REVOKE, "invited-person"))


def _request(steps=STEPS, **changes):
    values = dict(actor_subject=ACTOR, project_ref=PROJECT, request_id=REQUEST, request_digest=DIGEST, steps=steps)
    values.update(changes)
    return LifecyclePlanAuthorizationRequest(**values)


def _allow(request, step, grants=("memories:read",)):
    return ProjectAuthorizationDecision.allow(request.step_request(step), delegable_grants=grants)


def _envelope(request, decisions=None):
    return LifecyclePlanAuthorization(request=request, decisions=tuple(
        decisions if decisions is not None else ((step.ref, _allow(request, step)) for step in request.steps)))


def test_every_step_covered_exactly_once_validates_and_keeps_its_own_bounds():
    request = _request()
    grants = {"p": ("a",), "c": ("b",), "claim": ()}
    envelope = _envelope(request, [(step.ref, _allow(request, step, grants[step.ref])) for step in STEPS])
    envelope.validate_for(request)
    assert envelope.allowed and envelope.refusal() == ""
    assert {ref: envelope.decision_for(ref).delegable_grants for ref in grants} == grants


@pytest.mark.parametrize("change", ["missing", "extra", "repeated"])
def test_a_missing_extra_or_repeated_step_is_refused(change):
    request = _request()
    decisions = [(step.ref, _allow(request, step)) for step in STEPS]
    if change == "missing":
        decisions = decisions[:-1]
    elif change == "extra":
        decisions.append(("other", _allow(request, STEPS[0])))
    else:
        decisions.append(decisions[0])
    with pytest.raises(ProjectAuthorizationError, match="lifecycle_plan_authorization_steps_mismatch"):
        _envelope(request, decisions).validate_for(request)


def test_an_envelope_for_another_request_is_refused():
    request = _request()
    other = _request(request_digest="e" * 64)
    with pytest.raises(ProjectAuthorizationError, match="lifecycle_plan_authorization_request_mismatch"):
        _envelope(other).validate_for(request)


@pytest.mark.parametrize("field,value,reason", [
    ("target_subject", "someone-else", "project_authorization_target_mismatch"),
    ("operation", PROJECT_PERSON_CONTROL_REVOKE, "project_authorization_operation_mismatch"),
    ("actor_subject", "another-admin", "project_authorization_actor_mismatch"),
    ("request_id", "plan-2", "project_authorization_request_id_mismatch"),
])
def test_a_decision_for_another_target_operation_actor_or_request_is_refused(field, value, reason):
    import dataclasses

    request = _request()
    decisions = [(step.ref, _allow(request, step)) for step in STEPS]
    decisions[1] = ("c", dataclasses.replace(decisions[1][1], **{field: value}))
    with pytest.raises(ProjectAuthorizationError, match=reason):
        _envelope(request, decisions).validate_for(request)


def test_a_denied_step_denies_the_plan_by_its_own_reason():
    request = _request()
    decisions = [(step.ref, _allow(request, step)) for step in STEPS]
    decisions[2] = ("claim", ProjectAuthorizationDecision.deny(request.step_request(STEPS[2]),
                                                               reason="project_last_admin"))
    envelope = _envelope(request, decisions)
    envelope.validate_for(request)
    assert not envelope.allowed and envelope.refusal() == "project_last_admin"
    with pytest.raises(ProjectAuthorizationError, match="project_last_admin"):
        envelope.decision_for("claim")


@pytest.mark.parametrize("changes,reason", [
    ({"request_digest": "x"}, "lifecycle_plan_request_digest_invalid"),
    ({"steps": ()}, "lifecycle_plan_steps_invalid"),
    ({"steps": (STEPS[0], STEPS[0])}, "lifecycle_plan_steps_invalid"),
    ({"steps": tuple(LifecyclePlanStep(f"s{i}", PROJECT_PERSON_CONTROL_CREATE, ACTOR) for i in range(9))},
     "lifecycle_plan_steps_invalid"),
    ({"steps": (LifecyclePlanStep("x", "project.everything", ACTOR),)}, "project_authorization_operation_invalid"),
    ({"actor_subject": ""}, "lifecycle_plan_actor_subject_missing"),
])
def test_a_malformed_plan_request_is_refused(changes, reason):
    with pytest.raises(ProjectAuthorizationError, match=reason):
        _request(**changes)


@pytest.mark.parametrize("decisions", ["empty", "partial"])
def test_an_empty_or_partial_envelope_cannot_exist_so_it_is_never_allowed(decisions):
    """EMain F1 on #632: fail closed by construction, not only when a caller remembers validate_for."""
    request = _request()
    chosen = [] if decisions == "empty" else [(STEPS[0].ref, _allow(request, STEPS[0]))]
    with pytest.raises(ProjectAuthorizationError, match="lifecycle_plan_authorization_steps_mismatch"):
        LifecyclePlanAuthorization(request=request, decisions=tuple(chosen))


def test_a_decision_for_the_wrong_step_cannot_be_wrapped():
    import dataclasses

    request = _request()
    decisions = [(step.ref, _allow(request, step)) for step in STEPS]
    decisions[0] = ("p", dataclasses.replace(decisions[0][1], operation=PROJECT_PERSON_CONTROL_CREATE))
    with pytest.raises(ProjectAuthorizationError, match="project_authorization_operation_mismatch"):
        LifecyclePlanAuthorization(request=request, decisions=tuple(decisions))

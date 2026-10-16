"""W374 slice (b): the authority calls name their scope as scope_ref and project_ref, for one transition.

Design note_dd2bb7d8, GO with conditions (claude-app@spark1): both fields carry
one value; a host answer naming both with different values is refused
(``scope_ref_mismatch``), never "pick one"; the echoed value is still checked
exactly against the one asked for; and the Hub never parses a scope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    ProjectAuthorizationError,
    ProjectAuthorizationRequest,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
BOARD = "problem-board@1-0"
SCOPE = "work:project:quickstart"


def _module():
    return load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")[1]


def _membership_answer(subject: str, **echo) -> dict:
    return {"ok": True, "membership": {"subject": subject, "role": "owner", "delegable_grants": ["work:admin"],
                                       "evidence": {}, **echo}}


def _resolver(module, answer):
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs["data"])
        return answer(kwargs["data"])

    return module.BundleOperationProjectMembershipResolver(
        bundle_id=BOARD, operation="project_membership_resolve", caller=caller), calls


def test_every_unsigned_authority_call_sends_both_fields_with_one_value() -> None:
    module = _module()
    seen = []

    async def caller(**kwargs):
        seen.append(kwargs["data"])
        return {"ok": True, "decision": {"allowed": True, "via": "project_admin", "grantor_subject": "owner-one",
                                         **{k: v for k, v in kwargs["data"].items() if k != "scope_ref"}}}

    asyncio.run(module.BundleOperationAgentCardAuthorizer(bundle_id=BOARD, caller=caller)
                .authorize_agent_card(access_id="aut_a", project_ref=SCOPE, action="write"))
    asyncio.run(module.BundleOperationControlCardAuthorizer(bundle_id=BOARD, caller=caller)
                .authorize_project_control_card(control_id="aut_c", project_ref=SCOPE, action="write"))
    resolver, calls = _resolver(module, lambda data: _membership_answer(data["subject"], project_ref=SCOPE))
    asyncio.run(resolver.resolve_project_membership(project_ref=SCOPE, subject="person-one"))

    for data in [*seen, *calls]:
        assert data["scope_ref"] == data["project_ref"] == SCOPE


@pytest.mark.parametrize("echo", [{"scope_ref": SCOPE}, {"project_ref": SCOPE},
                                  {"scope_ref": SCOPE, "project_ref": SCOPE}],
                         ids=["scope_ref-only (new host)", "project_ref-only (old host)", "both, equal"])
def test_the_membership_echo_may_name_either_field(echo) -> None:
    resolver, _ = _resolver(_module(), lambda data: _membership_answer(data["subject"], **echo))
    evidence = asyncio.run(resolver.resolve_project_membership(project_ref=SCOPE, subject="person-one"))
    evidence.validate_for(project_ref=SCOPE, subject="person-one")


def test_an_echo_naming_two_different_scopes_is_refused_not_chosen() -> None:
    answer = lambda data: _membership_answer(data["subject"], scope_ref=SCOPE, project_ref="work:project:other")
    resolver, _ = _resolver(_module(), answer)
    with pytest.raises(ProjectAuthorizationError) as refused:
        asyncio.run(resolver.resolve_project_membership(project_ref=SCOPE, subject="person-one"))
    assert refused.value.reason == "scope_ref_mismatch"


@pytest.mark.asyncio
async def test_an_echo_of_a_different_scope_is_denied_exactly_as_a_wrong_project_ref_was() -> None:
    module = _module()

    async def caller(**kwargs):
        return {"project_membership_resolve": _membership_answer(kwargs["data"]["subject"],
                                                                 scope_ref="work:project:other")}

    entrypoint = SimpleNamespace(bundle_props={"authorities": {"membership": {
        "bundle_id": BOARD, "operation": "project_membership_resolve", "administrative_roles": ["owner"]}}})
    port = module.descriptor_project_authorization_port(entrypoint, caller=caller)
    decision = await port.authorize_project_person_control(ProjectAuthorizationRequest.build(
        actor_subject="admin-one", project_ref=SCOPE, target_subject="person-two",
        operation=PROJECT_PERSON_CONTROL_CREATE, request_id="request-1"))

    assert decision.allowed is False and decision.reason == "project_membership_project_ref_mismatch"


def test_the_hub_never_parses_a_scope() -> None:
    """Only the host knows what a scope looks like; any value goes out and comes back unchanged."""

    opaque = "tenant-a/space:42?not-a-project"
    resolver, calls = _resolver(_module(), lambda data: _membership_answer(data["subject"], scope_ref=opaque))
    evidence = asyncio.run(resolver.resolve_project_membership(project_ref=opaque, subject="person-one"))
    assert calls == [{"scope_ref": opaque, "project_ref": opaque, "subject": "person-one"}]
    evidence.validate_for(project_ref=opaque, subject="person-one")

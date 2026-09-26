"""The project path to an agent's Card, at the app boundary (W319).

Connection Hub asks the project host (Problem Board) "may this person open or
change this agent's Card" under the person's session, and serves three
operations that act through that answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from connection_hub.delegated_credentials.project_agent_card_access import (
    AgentCardAuthorizationError,
    AgentCardDecision,
    RefusingAgentCardAuthorizationPort,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ("project_agent_card_get", "project_agent_card_update", "project_agent_card_apply_profile")


def _entrypoint_module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "entrypoint.py")
    return module


def _membership_module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")
    return module


def test_the_agent_card_question_goes_to_the_project_host_under_the_session():
    module = _membership_module()
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "decision": {
            "allowed": True, "via": "project_admin", "grantor_subject": "owner-one",
            "access_id": "aut_a", "project_ref": "work:project:one", "action": "write",
        }}

    port = module.BundleOperationAgentCardAuthorizer(bundle_id="problem-board@1-0", caller=caller)
    decision = asyncio.run(port.authorize_agent_card(access_id="aut_a", project_ref="work:project:one", action="write"))
    assert calls == [{"bundle_id": "problem-board@1-0", "operation": "project_agent_card_authorize",
                      "data": {"access_id": "aut_a", "project_ref": "work:project:one", "action": "write"}}]
    assert decision.allowed and decision.via == "project_admin" and decision.grantor_subject == "owner-one"


def test_host_refusals_and_failures():
    module = _membership_module()

    async def refusing(**_kwargs):
        return {"ok": False, "error": {"code": "work_identity_required"}}

    refused = asyncio.run(module.BundleOperationAgentCardAuthorizer(bundle_id="pb", caller=refusing)
                          .authorize_agent_card(access_id="aut_a", project_ref="", action="read"))
    assert refused.allowed is False and refused.reason == "work_identity_required"

    async def broken(**_kwargs):
        raise RuntimeError("down")

    with pytest.raises(AgentCardAuthorizationError) as down:
        asyncio.run(module.BundleOperationAgentCardAuthorizer(bundle_id="pb", caller=broken)
                    .authorize_agent_card(access_id="aut_a", project_ref="", action="read"))
    assert down.value.reason == "project_agent_card_provider_unavailable"

    async def bad(**_kwargs):
        return {"ok": True, "decision": {"allowed": True, "via": "owner"}}

    with pytest.raises(AgentCardAuthorizationError):
        asyncio.run(module.BundleOperationAgentCardAuthorizer(bundle_id="pb", caller=bad)
                    .authorize_agent_card(access_id="aut_a", project_ref="", action="read"))


def test_the_descriptor_names_the_operation_and_fails_closed_without_a_provider():
    module = _membership_module()
    configured = SimpleNamespace(bundle_props={"project_membership": {"provider": {
        "bundle_id": "problem-board@1-0", "operation": "project_membership_resolve",
        "agent_card_operation": "project_agent_card_authorize"}}})
    port = module.descriptor_agent_card_port(configured)
    assert isinstance(port, module.BundleOperationAgentCardAuthorizer)
    assert isinstance(module.descriptor_agent_card_port(SimpleNamespace(bundle_props={})), RefusingAgentCardAuthorizationPort)

    template = yaml.safe_load((BUNDLE_ROOT / "config" / "bundles.template.yaml").read_text())
    text = yaml.safe_dump(template)
    assert "agent_card_operation: project_agent_card_authorize" in text


def test_the_three_operations_are_csrf_protected_posts():
    module = _entrypoint_module()
    for alias in OPERATIONS:
        method = getattr(getattr(module.ConnectionHubEntrypoint, alias), "__bundle_api_method__")
        assert method.alias == alias and method.http_method == "POST"
        assert alias in module.CSRF_PROTECTED_OPERATION_ALIASES


def test_the_operations_pass_the_person_and_the_body_to_the_project_path(monkeypatch):
    module = _entrypoint_module()
    calls = []

    class Access:
        async def get(self, user, **kwargs):
            calls.append(("get", user, kwargs))
            return {"ok": True}

        async def update(self, user, **kwargs):
            calls.append(("update", user, kwargs))
            return {"ok": True}

        async def apply_profile(self, user, **kwargs):
            calls.append(("apply_profile", user, kwargs))
            return {"ok": True}

    async def access(_self, _request):
        return Access()

    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_project_agent_card_access", access)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "second-admin"})
    monkeypatch.setattr(module, "_audit_request_id", lambda _request: "req-1")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    base = {"access_id": " aut_a ", "project_ref": "work:project:one"}

    asyncio.run(module.ConnectionHubEntrypoint.project_agent_card_get(instance, data=base))
    asyncio.run(module.ConnectionHubEntrypoint.project_agent_card_update(
        instance, data={**base, "resource_grants": {"problem_board": ["work:relay"]}, "expected_card_revision": 4}))
    asyncio.run(module.ConnectionHubEntrypoint.project_agent_card_apply_profile(
        instance, data={**base, "profile": "coordinator", "expected_card_revision": 5}))

    assert [name for name, *_ in calls] == ["get", "update", "apply_profile"]
    assert all(user == {"user_id": "second-admin"} for _, user, _ in calls)
    assert calls[0][2] == {"access_id": "aut_a", "project_ref": "work:project:one"}
    assert calls[1][2]["resource_grants"] == {"problem_board": ["work:relay"]}
    assert calls[1][2]["expected_card_revision"] == 4 and calls[1][2]["request_id"] == "req-1"
    assert calls[2][2]["profile"] == "coordinator" and calls[2][2]["expected_card_revision"] == 5


def test_a_decision_needs_its_owner():
    assert AgentCardDecision.from_mapping({"allowed": False}).allowed is False


SHARE_OPERATIONS = ("agent_card_share", "agent_card_unshare", "agent_card_shares")


def test_the_share_operations_are_posts_and_only_the_read_is_csrf_exempt():
    module = _entrypoint_module()
    for alias in (*SHARE_OPERATIONS, "agent_card_shared_with_me"):
        method = getattr(getattr(module.ConnectionHubEntrypoint, alias), "__bundle_api_method__")
        assert method.alias == alias and method.http_method == "POST"
    assert set(SHARE_OPERATIONS) <= module.CSRF_PROTECTED_OPERATION_ALIASES
    assert "agent_card_shared_with_me" in module.CSRF_EXEMPT_POST_OPERATION_ALIASES
    assert "agent_card_shared_with_me" not in module.CSRF_PROTECTED_OPERATION_ALIASES


def test_the_share_operations_pass_the_person_and_fail_closed_without_storage(monkeypatch):
    module = _entrypoint_module()
    calls = []

    class Shares:
        async def share(self, user, **kwargs):
            calls.append(("share", user, kwargs))
            return {"ok": True}

        async def unshare(self, user, **kwargs):
            calls.append(("unshare", user, kwargs))
            return {"ok": True}

        async def shares(self, user, **kwargs):
            calls.append(("shares", user, kwargs))
            return {"ok": True}

        async def shared_with_me(self, user):
            calls.append(("shared_with_me", user, {}))
            return {"ok": True}

    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "owner-one"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_agent_card_shares", lambda _self: Shares())
    E = module.ConnectionHubEntrypoint
    asyncio.run(E.agent_card_share(instance, data={"access_id": " aut_a ", "grantee_subject": "ada", "level": "edit"}))
    asyncio.run(E.agent_card_unshare(instance, data={"access_id": "aut_a", "grantee_subject": "ada"}))
    asyncio.run(E.agent_card_shares(instance, data={"access_id": "aut_a"}))
    asyncio.run(E.agent_card_shared_with_me(instance, data={}))
    assert [name for name, *_ in calls] == ["share", "unshare", "shares", "shared_with_me"]
    assert calls[0][2] == {"access_id": "aut_a", "grantee_subject": "ada", "level": "edit"}
    assert all(user == {"user_id": "owner-one"} for _, user, _ in calls)

    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_agent_card_shares", lambda _self: None)
    down = asyncio.run(E.agent_card_shared_with_me(instance, data={}))
    assert down["ok"] is False and down["error"] == "agent_card_shares_unavailable"

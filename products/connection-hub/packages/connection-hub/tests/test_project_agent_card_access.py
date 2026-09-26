"""The project path to an agent's Card (W319, slice 1).

Connection Hub stores and changes a Card under its owner. The project host
(Problem Board) answers whether a person may open or change an agent's Card:
the owner and an admin of a project the agent attends now may do both, a
platform admin may only open it. This service then acts under the owner's
storage key and records the acting person.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from connection_hub.delegated_credentials.project_agent_card_access import (
    PROJECT_AGENT_CARD_AUDIT_PROVENANCE,
    AgentCardAuthorizationError,
    AgentCardDecision,
    ProjectAgentCardAccess,
    RefusingAgentCardAuthorizationPort,
)

ACCESS = "aut_agent"
PROJECT = "work:project:one"


class Port:
    def __init__(self, table: dict[tuple[str, str], AgentCardDecision] | None = None, fail: Exception | None = None):
        self.table = table or {}
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    async def authorize_agent_card(self, *, access_id, project_ref, action):
        self.calls.append({"access_id": access_id, "project_ref": project_ref, "action": action})
        if self.fail is not None:
            raise self.fail
        return self.table.get((project_ref, action), AgentCardDecision(allowed=False, reason="work_agent_card_read_denied" if action == "read" else "work_agent_card_write_denied", message="no"))


@dataclass
class Record:
    card_revision: int = 1
    provenance: dict = field(default_factory=dict)


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_access(self, user):
        self.calls.append(("list_access", dict(user)))
        return {"ok": True, "items": [{"access_id": ACCESS, "grantor_subject": "owner-one"}, {"access_id": "aut_other"}]}

    async def grant_options(self, user):
        return [{"grant": "work:relay"}]

    async def resource_options(self, user):
        return [{"resource": "problem_board"}]

    async def _offer_config(self, *, owner_subject):
        return SimpleNamespace(owner=owner_subject)

    async def _available_inventory(self, user, *, config):
        roles = set(user.get("roles") or [])
        names = ["work:relay"] + (["work:coordinate"] if "kdcube:role:registered" in roles else [])
        return SimpleNamespace(grant_names=lambda: names)

    async def update_access(self, user, **kwargs):
        transformed = kwargs["_record_transform"](Record(card_revision=3), Record(card_revision=4))
        self.calls.append(("update_access", {"user": dict(user), **kwargs, "transformed": transformed}))
        return {"ok": True, "card_revision": 4}

    async def apply_authorization_profile(self, user, **kwargs):
        transformed = kwargs["_extra_record_transform"](Record(card_revision=4), Record(card_revision=5))
        self.calls.append(("apply_authorization_profile", {"user": dict(user), **kwargs, "transformed": transformed}))
        return {"ok": True, "profile": kwargs["profile"]}


def _allow(via, action, project_ref=PROJECT):
    return AgentCardDecision(allowed=True, via=via, grantor_subject="owner-one", access_id=ACCESS,
                             project_ref=project_ref, action=action, worker_name="claude-code-agent")


ADA = {"user_id": "ada", "roles": ["kdcube:role:registered"], "permissions": []}
ROOT = {"user_id": "root", "roles": ["kdcube:role:super-admin"], "permissions": []}


def test_a_project_admin_opens_the_card_under_the_owners_key_and_may_edit():
    host, port = Host(), Port({(PROJECT, "read"): _allow("project_admin", "read")})
    result = asyncio.run(ProjectAgentCardAccess(host, port).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert result["ok"] is True and result["item"]["access_id"] == ACCESS
    assert result["access"] == {"via": "project_admin", "can_edit": True, "project_ref": PROJECT, "worker_name": "claude-code-agent"}
    assert host.calls[0] == ("list_access", {"user_id": "owner-one", "roles": [], "permissions": []})


def test_a_platform_admin_reads_but_may_not_edit():
    host = Host()
    port = Port({("", "read"): _allow("platform_admin", "read", project_ref="")})
    access = ProjectAgentCardAccess(host, port)
    result = asyncio.run(access.get(ROOT, access_id=ACCESS, project_ref=""))
    assert result["ok"] is True and result["access"]["can_edit"] is False and result["access"]["via"] == "platform_admin"
    assert [call["action"] for call in port.calls] == ["read", "write"], "can_edit asks the host, never assumes"

    refused = asyncio.run(access.update(ROOT, access_id=ACCESS, project_ref="", resource_grants={}))
    assert refused == {"ok": False, "error": "work_agent_card_write_denied", "status": 403, "message": "no"}
    assert not [call for call in host.calls if call[0] == "update_access"]


def test_a_project_admin_changes_the_card_audited_within_what_they_could_delegate():
    host, port = Host(), Port({(PROJECT, "write"): _allow("project_admin", "write")})
    result = asyncio.run(ProjectAgentCardAccess(host, port).update(
        ADA, access_id=ACCESS, project_ref=PROJECT, request_id="req-1", resource_grants={"problem_board": ["work:relay"]},
    ))
    assert result["ok"] is True
    name, call = host.calls[-1]
    assert name == "update_access" and call["user"]["user_id"] == "owner-one", "stored under the owner"
    assert call["_platform_admin"] is False
    assert call["_delegable_grants"] == ["work:coordinate", "work:relay"], "the acting admin's inventory"
    audit = call["transformed"].provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]
    assert audit["actor_subject"] == "ada" and audit["via"] == "project_admin" and audit["project_ref"] == PROJECT
    assert (audit["before_revision"], audit["after_revision"]) == (3, 4) and audit["request_id"] == "req-1"


def test_apply_profile_goes_through_the_same_path_with_the_actor_named():
    host, port = Host(), Port({(PROJECT, "write"): _allow("project_admin", "write")})
    result = asyncio.run(ProjectAgentCardAccess(host, port).apply_profile(
        ADA, access_id=ACCESS, project_ref=PROJECT, profile="coordinator", expected_card_revision=4,
    ))
    assert result == {"ok": True, "profile": "coordinator"}
    _, call = host.calls[-1]
    assert call["user"]["user_id"] == "owner-one" and call["_actor_subject"] == "ada"
    assert call["transformed"].provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]["action"] == "profile_applied"


def test_refusals_unavailability_and_bad_requests():
    host = Host()
    denied = asyncio.run(ProjectAgentCardAccess(host, Port()).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert denied["status"] == 403 and denied["error"] == "work_agent_card_read_denied"

    down = asyncio.run(ProjectAgentCardAccess(host, Port(fail=RuntimeError("down"))).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert down["status"] == 503 and down["retryable"] is True
    bad_answer = asyncio.run(ProjectAgentCardAccess(host, Port(fail=AgentCardAuthorizationError("x"))).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert bad_answer["status"] == 503 and bad_answer["reason"] == "x"

    none = asyncio.run(ProjectAgentCardAccess(host, None).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert none["status"] == 503 and none["reason"] == "authorization_port_not_configured"
    closed = asyncio.run(ProjectAgentCardAccess(host, RefusingAgentCardAuthorizationPort("not_configured")).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    # Review on app-ecosystem#187: a deployment gap is unavailable, not a denial.
    assert closed["status"] == 503 and closed["reason"] == "not_configured" and closed["retryable"] is True

    delegate = asyncio.run(ProjectAgentCardAccess(host, Port()).get({"user_id": "integration:c:owner-one"}, access_id=ACCESS, project_ref=PROJECT))
    assert delegate["status"] == 401
    blank = asyncio.run(ProjectAgentCardAccess(host, Port()).get(ADA, access_id=" ", project_ref=PROJECT))
    assert blank["status"] == 400


def test_a_decision_for_another_card_is_not_trusted():
    host = Host()
    wrong = AgentCardDecision(allowed=True, via="project_admin", grantor_subject="owner-one", access_id="aut_other", project_ref=PROJECT, action="read")
    result = asyncio.run(ProjectAgentCardAccess(host, Port({(PROJECT, "read"): wrong})).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert result["status"] == 503 and result["reason"] == "decision_access_id_mismatch"


def test_an_allowed_decision_must_name_the_owner():
    try:
        AgentCardDecision.from_mapping({"allowed": True, "via": "project_admin"})
    except AgentCardAuthorizationError as exc:
        assert exc.reason == "project_agent_card_decision_grantor_missing"
    else:
        raise AssertionError("an allowed decision without an owner must be refused")


def test_a_decision_must_name_this_card_and_this_action_and_a_write_must_come_from_an_editor():
    # Review on app-ecosystem#161: fail closed on a missing or different access_id,
    # a different action, and a write allowed by a via that cannot edit.
    host = Host()
    missing = AgentCardDecision(allowed=True, via="project_admin", grantor_subject="owner-one", access_id="",
                                project_ref=PROJECT, action="read")
    result = asyncio.run(ProjectAgentCardAccess(host, Port({(PROJECT, "read"): missing})).get(ADA, access_id=ACCESS, project_ref=PROJECT))
    assert result["status"] == 503 and result["reason"] == "decision_access_id_mismatch"

    other_action = _allow("project_admin", "read")
    result = asyncio.run(ProjectAgentCardAccess(host, Port({(PROJECT, "write"): other_action})).update(
        ADA, access_id=ACCESS, project_ref=PROJECT, resource_grants={"problem_board": ["work:relay"]},
    ))
    assert result["status"] == 503 and result["reason"] == "decision_action_mismatch"

    reader = _allow("platform_admin", "write")
    for call in (
        lambda access: access.update(ROOT, access_id=ACCESS, project_ref=PROJECT, resource_grants={"problem_board": ["work:relay"]}),
        lambda access: access.apply_profile(ROOT, access_id=ACCESS, project_ref=PROJECT, profile="coordinator"),
    ):
        refused = asyncio.run(call(ProjectAgentCardAccess(host, Port({(PROJECT, "write"): reader}))))
        assert refused["status"] == 403 and refused["reason"] == "decision_via_cannot_edit"
    assert not any(name in {"update_access", "apply_authorization_profile"} for name, _ in host.calls)

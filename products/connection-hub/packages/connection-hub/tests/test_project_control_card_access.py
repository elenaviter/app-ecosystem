"""The project path to a project's Control Card (W260).

Connection Hub stores a project's Control Card under the person who created it,
and the creator's own ``control_card_get``/``control_card_update`` find it only
among the caller's Cards, so every other admin was answered
``control_card_not_found`` (applications#160 review). The project host
(Problem Board) answers whether a person may read or change the project's
Control Card and names its creator; this service then acts under the creator's
storage key, bounded by what the acting person could delegate, and records who
acted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.project_control_card_access import (
    PROJECT_CONTROL_CARD_AUDIT_PROVENANCE,
    ControlCardAuthorizationError,
    ProjectControlCardAccess,
    ProjectControlCardDecision,
    RefusingProjectControlCardAuthorizationPort,
)

CONTROL = "aut_control"
PROJECT = "work:project:one"
CREATOR = "boris"


class Port:
    def __init__(self, table=None, fail: Exception | None = None):
        self.table = table or {}
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    async def authorize_project_control_card(self, *, control_id, project_ref, action, access_id=""):
        self.calls.append({"control_id": control_id, "project_ref": project_ref, "action": action})
        if self.fail is not None:
            raise self.fail
        return self.table.get(
            action,
            ProjectControlCardDecision(
                allowed=False, reason=f"work_project_control_card_{action}_denied", message="Ask an admin.",
                control_id=control_id, project_ref=project_ref, action=action,
            ),
        )


@dataclass
class Record:
    card_revision: int = 1
    provenance: dict = field(default_factory=dict)


class Host:
    def __init__(self, *, found: bool = True) -> None:
        self.found = found
        self.calls: list[tuple[str, dict]] = []

    async def control_card_get(self, user, *, control_id):
        self.calls.append(("control_card_get", {"user": dict(user), "control_id": control_id}))
        if not self.found:
            return {"ok": False, "error": "control_card_not_found", "status": 404}
        return {"ok": True, "control_card": {"control_id": control_id}, "access": {"access_id": control_id}}

    async def _offer_config(self, *, owner_subject):
        return SimpleNamespace(owner=owner_subject)

    async def _available_inventory(self, user, *, config):
        roles = set(user.get("roles") or [])
        names = ["work:relay"] + (["work:coordinate"] if "kdcube:role:registered" in roles else [])
        return SimpleNamespace(grant_names=lambda: names)

    async def control_card_update(self, user, **kwargs):
        transformed = kwargs["_record_transform"](Record(card_revision=3), Record(card_revision=4))
        self.calls.append(("control_card_update", {"user": dict(user), **kwargs, "transformed": transformed}))
        if not self.found:
            return {"ok": False, "error": "control_card_not_found", "status": 404}
        return {"ok": True, "control_card": {"control_id": kwargs["control_id"]}, "access": {"card_revision": 4}}


def _allow(via, action, changes=None):
    values = dict(allowed=True, via=via, grantor_subject=CREATOR, control_id=CONTROL, project_ref=PROJECT,
                  action=action, evidence={"role": "admin", "operation": "project.control.update"})
    values.update(changes or {})
    return ProjectControlCardDecision(**values)


ADA = {"user_id": "ada", "roles": ["kdcube:role:registered"], "permissions": []}


def _get(host, port, user=ADA, **kwargs):
    kwargs = {"control_id": CONTROL, "project_ref": PROJECT, **kwargs}
    return asyncio.run(ProjectControlCardAccess(host, port).get(user, **kwargs))


def _update(host, port, user=ADA, **kwargs):
    kwargs = {"control_id": CONTROL, "project_ref": PROJECT, "request_id": "req-1",
              "resource_grants": {"problem_board": ["work:relay"]}, **kwargs}
    return asyncio.run(ProjectControlCardAccess(host, port).update(user, **kwargs))


def test_a_project_admin_opens_the_control_card_under_the_creators_key_and_may_edit():
    host, port = Host(), Port({"read": _allow("project_admin", "read")})
    result = _get(host, port)
    assert result["ok"] is True and result["control_card"]["control_id"] == CONTROL
    assert result["access"]["via"] == "project_admin" and result["access"]["can_edit"] is True
    assert result["access"]["project_ref"] == PROJECT
    assert host.calls == [("control_card_get", {"user": {"user_id": CREATOR, "roles": [], "permissions": []},
                                                "control_id": CONTROL})]
    assert [call["action"] for call in port.calls] == ["read"]


def test_a_member_reads_but_may_not_edit():
    host = Host()
    port = Port({"read": _allow("project_member", "read")})
    result = _get(host, port)
    assert result["ok"] is True and result["access"]["can_edit"] is False
    assert [call["action"] for call in port.calls] == ["read", "write"], "can_edit asks the host, never assumes"

    refused = _update(host, port)
    assert refused["status"] == 403 and refused["error"] == "work_project_control_card_write_denied"
    assert refused["message"] == "Ask an admin."
    assert not [call for call in host.calls if call[0] == "control_card_update"]


def test_a_member_answer_for_a_write_is_refused_whatever_the_host_says():
    host, port = Host(), Port({"write": _allow("project_member", "write")})
    refused = _update(host, port)
    assert refused == {"ok": False, "error": "project_control_card_write_denied",
                       "reason": "decision_via_cannot_edit", "status": 403}
    assert host.calls == []


def test_an_editor_changes_the_card_audited_within_what_they_could_delegate():
    host, port = Host(), Port({"write": _allow("project_admin", "write")})
    result = _update(host, port)
    assert result["ok"] is True and result["access"]["can_edit"] is True
    name, call = host.calls[-1]
    assert name == "control_card_update" and call["user"]["user_id"] == CREATOR, "stored under the creator"
    assert call["control_id"] == CONTROL and call["resource_grants"] == {"problem_board": ["work:relay"]}
    assert call["_delegable_grants"] == ["work:coordinate", "work:relay"], "the acting person's inventory"
    audit = call["transformed"].provenance[PROJECT_CONTROL_CARD_AUDIT_PROVENANCE]
    assert audit["actor_subject"] == "ada" and audit["via"] == "project_admin" and audit["project_ref"] == PROJECT
    assert (audit["before_revision"], audit["after_revision"]) == (3, 4) and audit["request_id"] == "req-1"
    assert audit["evidence"] == {"role": "admin", "operation": "project.control.update"}


def test_the_owner_edits_through_the_same_path():
    host, port = Host(), Port({"write": _allow("owner", "write")})
    assert _update(host, port)["ok"] is True
    assert host.calls[-1][1]["transformed"].provenance[PROJECT_CONTROL_CARD_AUDIT_PROVENANCE]["via"] == "owner"


def test_a_stale_link_is_not_found_and_names_nobody():
    host, port = Host(found=False), Port({"read": _allow("project_admin", "read"), "write": _allow("project_admin", "write")})
    assert _get(host, port) == {"ok": False, "error": "project_control_card_not_found", "status": 404}
    assert _update(host, port) == {"ok": False, "error": "project_control_card_not_found", "status": 404}


def test_host_refusals_reach_the_person_with_their_words():
    refused = _get(Host(), Port())
    assert refused == {"ok": False, "error": "work_project_control_card_read_denied", "status": 403,
                       "message": "Ask an admin."}


@pytest.mark.parametrize(
    ("port", "reason"),
    [
        (None, "authorization_port_not_configured"),
        (Port(fail=RuntimeError("down")), "authorization_port_failed"),
        (Port(fail=ControlCardAuthorizationError("project_control_card_provider_unavailable")),
         "project_control_card_provider_unavailable"),
    ],
)
def test_an_unreachable_host_is_unavailable_never_allowed(port, reason):
    host = Host()
    result = _get(host, port)
    assert result["status"] == 503 and result["error"] == "project_control_card_authorization_unavailable"
    assert result["reason"] == reason and result["retryable"] is True
    assert host.calls == []


def test_an_unconfigured_provider_is_unavailable_not_a_denial():
    # Review on app-ecosystem#187: a deployment gap must not read like "you may not".
    host = Host()
    port = RefusingProjectControlCardAuthorizationPort("project_control_card_provider_not_configured")
    result = _get(host, port)
    assert result == {"ok": False, "error": "project_control_card_authorization_unavailable",
                      "reason": "project_control_card_provider_not_configured", "retryable": True, "status": 503}
    assert host.calls == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"control_id": "aut_other"}, "decision_control_id_mismatch"),
        ({"project_ref": "work:project:other"}, "decision_project_ref_mismatch"),
        ({"action": "write"}, "decision_action_mismatch"),
        ({"grantor_subject": ""}, "decision_grantor_missing"),
        ({"via": "platform_admin"}, "decision_via_invalid"),
    ],
)
def test_an_answer_about_something_else_fails_closed_before_storage(changes, reason):
    host = Host()
    result = _get(host, Port({"read": _allow("project_admin", "read", changes)}))
    assert result == {"ok": False, "error": "project_control_card_authorization_invalid",
                      "reason": reason, "retryable": True, "status": 503}
    assert host.calls == []


def test_only_a_person_with_both_ids_is_asked():
    host, port = Host(), Port({"read": _allow("project_admin", "read")})
    integration = {"user_id": "integration:relay", "roles": [], "permissions": []}
    assert _get(host, port, user=integration)["status"] == 401
    assert _get(host, port, control_id="")["error"] == "project_control_card_request_invalid"
    assert _get(host, port, project_ref=" ")["status"] == 400
    assert port.calls == [] and host.calls == []


def test_a_decision_is_read_strictly():
    with pytest.raises(ControlCardAuthorizationError):
        ProjectControlCardDecision.from_mapping({"allowed": "yes"})
    with pytest.raises(ControlCardAuthorizationError):
        ProjectControlCardDecision.from_mapping({"allowed": True, "evidence": ["role"]})
    decision = ProjectControlCardDecision.from_mapping(
        {"allowed": True, "via": " owner ", "grantor_subject": CREATOR, "control_id": CONTROL,
         "project_ref": PROJECT, "action": "read"}
    )
    assert decision.via == "owner" and decision.evidence == {}


# -- the same path on the real Card service -----------------------------------


def _real_card():
    from test_agent_capability_control_sync import NAMED_RESOURCE, _service

    service, _ = _service(named_services=True)
    creator = {"user_id": CREATOR, "roles": ["kdcube:role:super-admin"], "permissions": []}
    made = asyncio.run(service.control_card_create(
        creator, issuer_ref=PROJECT, issuer_kind="project", issuer_label="One",
    ))
    assert made["ok"] is True
    return service, creator, made["control_card"]["access_id"], NAMED_RESOURCE


def _real_port(control_id, via="project_admin"):
    return Port({
        action: _allow(via, action, {"control_id": control_id}) for action in ("read", "write")
    })


def test_a_second_admin_edits_the_creators_control_card_on_the_real_service():
    service, creator, control_id, resource = _real_card()
    admin = {"user_id": "root", "roles": ["kdcube:role:super-admin"], "permissions": []}
    # The creator's own path still finds only the creator's Cards.
    assert asyncio.run(service.control_card_get(admin, control_id=control_id))["error"] == "control_card_not_found"

    access = ProjectControlCardAccess(service, _real_port(control_id))
    opened = asyncio.run(access.get(admin, control_id=control_id, project_ref=PROJECT))
    assert opened["ok"] is True and opened["access"]["can_edit"] is True

    changed = asyncio.run(access.update(
        admin, control_id=control_id, project_ref=PROJECT, request_id="req-real",
        resource_grants={resource: ["named_services:use", "slack:read"]},
    ))
    assert changed["ok"] is True, changed
    stored = asyncio.run(service.control_card_get(creator, control_id=control_id))
    assert stored["access"]["resource_grants"][resource] == ["named_services:use", "slack:read"]
    assert stored["access"]["grantor_subject"] == CREATOR, "still the creator's Card"
    audit = stored["access"]["provenance"][PROJECT_CONTROL_CARD_AUDIT_PROVENANCE]
    assert audit["actor_subject"] == "root" and audit["via"] == "project_admin"
    assert audit["request_id"] == "req-real" and audit["after_revision"] == audit["before_revision"] + 1


def test_an_editor_asking_for_more_than_they_hold_is_refused_by_name():
    service, creator, control_id, resource = _real_card()
    ada = {"user_id": "ada", "roles": ["kdcube:role:registered"], "permissions": []}
    access = ProjectControlCardAccess(service, _real_port(control_id))
    refused = asyncio.run(access.update(
        ada, control_id=control_id, project_ref=PROJECT, request_id="req-wide",
        resource_grants={resource: ["named_services:use", "slack:post"]},
    ))
    assert refused["ok"] is False and refused["error"] == "delegated_access_grants_not_delegable"
    assert sorted(refused["grants"]) == ["named_services:use", "slack:post"]
    # The refusal says the save needs every permission the Card carries.
    assert "every permission it carries, not only the ones you change" in refused["message"]
    assert "named_services:use" in refused["message"] and "slack:post" in refused["message"]
    stored = asyncio.run(service.control_card_get(creator, control_id=control_id))
    assert PROJECT_CONTROL_CARD_AUDIT_PROVENANCE not in (stored["access"].get("provenance") or {})
    assert "slack:post" not in (stored["access"].get("resource_grants") or {}).get(resource, [])


def test_an_editor_with_fewer_grants_than_the_card_cannot_save_even_an_unrelated_change():
    """Review on app-ecosystem#187: the save is checked against every grant the Card carries."""

    service, creator, control_id, resource = _real_card()
    asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={resource: ["named_services:use", "slack:read"]},
    ))
    ada = {"user_id": "ada", "roles": ["kdcube:role:registered"], "permissions": []}
    access = ProjectControlCardAccess(service, _real_port(control_id))
    refused = asyncio.run(access.update(
        ada, control_id=control_id, project_ref=PROJECT, request_id="req-label", label="Renamed",
    ))
    assert refused["error"] == "delegated_access_grants_not_delegable"
    assert "not only the ones you change" in refused["message"]
    stored = asyncio.run(service.control_card_get(creator, control_id=control_id))
    assert stored["access"]["label"] != "Renamed"


def test_a_control_card_not_held_by_a_project_stays_editable_by_its_creator():
    """The operator's scope (2026-09-26): only Control Cards that belong to a
    Problem Board project are edited through the project. Any other Control
    Card (for example one governing a hosted agent's capabilities) keeps its
    normal Connection Hub editing by its creator, with no project question."""

    from test_agent_capability_control_sync import NAMED_RESOURCE, _service

    service, _ = _service(named_services=True)
    creator = {"user_id": CREATOR, "roles": ["kdcube:role:super-admin"], "permissions": []}
    made = asyncio.run(service.control_card_create(
        creator, issuer_ref="agent:resident:helper", issuer_kind="application", issuer_label="Helper",
    ))
    control_id = made["control_card"]["access_id"]
    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
    ))
    assert changed["ok"] is True, changed
    opened = asyncio.run(service.control_card_get(creator, control_id=control_id))
    assert opened["access"]["resource_grants"][NAMED_RESOURCE] == ["named_services:use", "slack:read"]
    assert "viewer" not in opened, "no project read-only view on a Card no project holds"

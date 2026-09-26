"""Attaching a project's Control Card to another person's agent, through the project (W260).

Connection Hub attached a Control Card only when the caller owned both Cards,
and composed a bound Card only with a Control Card of the same grantor. A
project's Control Card is kept under the person who created it, so every
other person's agent could never carry it. The project host now decides the
attach (the Card's side) and W319's agent question decides the agent's side;
the binding records the Control Card's holder, and the runtime resolves it
under that holder. Composition stays AND: another person's Control Card only
narrows the agent.
"""

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    card_authority_from_record,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CONTROL_COMPOSITION_AND,
    ControlCardBinding,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.project_agent_card_access import (
    AgentCardDecision,
    ProjectAgentCardAccess,
)
from connection_hub.delegated_credentials.project_control_card_access import (
    PROJECT_CONTROL_CARD_ATTACH_AUDIT_PROVENANCE,
    ProjectControlCardAccess,
    ProjectControlCardDecision,
)
from test_credentialless_card_persistence import (
    RESOURCE,
    _agent,
    _Handles,
    _persistence,
)
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles

OWNER = "owner-o"  # owns the agent
CREATOR = "creator-c"  # created the project's Control Card
ADMIN = {"user_id": "admin-a", "roles": ["kdcube:role:registered"], "permissions": []}
PROJECT = "work:project:demo"
CONTROL_ID = "control-project-1"


def _cards(*, control_operations=None, control_state=CARD_STATE_ACTIVE):
    agent = dataclasses.replace(
        _agent(),
        grantor_subject=OWNER,
        resource_grants={RESOURCE: ("messages:read",)},
        resource_operations={RESOURCE: ("messages.search",)},
    )
    control = new_credentialless_card(
        initial_selection=agent,
        grantor_subject=CREATOR,
        catalog_version=agent.catalog_version,
        control_id=CONTROL_ID,
        issuer_ref=PROJECT,
        issuer_kind="application",
        issuer_label="Demo project",
        manage_url="/problem-board/projects/demo/control",
        now=int(time.time()),
    )
    if control_operations is not None:
        control = dataclasses.replace(control, resource_operations={RESOURCE: tuple(control_operations)})
    if control_state != CARD_STATE_ACTIVE:
        control = dataclasses.replace(control, state=control_state)
    return agent, control


def _service(agent, control):
    authorities = {agent.access_id: agent, control.access_id: control}
    handles = _Handles({
        agent.access_id: CardCredentialHandles(
            access_id=agent.access_id, access_token="token", session_id="session-1",
        )
    })
    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=None,  # type: ignore[arg-type]
        grant_store=object(),
        card_persistence=_persistence(authorities, handles),
    )
    service.notify_change = AsyncMock()
    return service, authorities


class ControlPort:
    def __init__(self, *, via="project_admin", allowed=True, grantor=CREATOR):
        self.via, self.allowed, self.grantor = via, allowed, grantor
        self.calls = []

    async def authorize_project_control_card(self, *, control_id, project_ref, action):
        self.calls.append(action)
        if not self.allowed:
            return ProjectControlCardDecision(
                allowed=False, reason="work_project_control_card_attach_denied", message="Ask an admin.",
                control_id=control_id, project_ref=project_ref, action=action,
            )
        return ProjectControlCardDecision(
            allowed=True, via=self.via, grantor_subject=self.grantor,
            control_id=control_id, project_ref=project_ref, action=action,
        )


class AgentPort:
    def __init__(self, *, via="project_admin_linking", allowed=True):
        self.via, self.allowed = via, allowed

    async def authorize_agent_card(self, *, access_id, project_ref, action):
        if not self.allowed:
            return AgentCardDecision(allowed=False, reason="work_agent_card_write_denied", message="no",
                                     access_id=access_id, project_ref=project_ref, action=action)
        return AgentCardDecision(allowed=True, via=self.via, grantor_subject=OWNER, access_id=access_id,
                                 project_ref=project_ref, action=action)


def _access(service, *, control_port=None, agent_port=None):
    return ProjectControlCardAccess(
        service,
        control_port or ControlPort(),
        agent_access=ProjectAgentCardAccess(service, agent_port or AgentPort()),
    )


async def _attach(service, access, agent):
    return await access.attach(
        ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id, request_id="req-attach",
    )


@pytest.mark.asyncio
async def test_a_second_admin_attaches_the_creators_control_card_to_anothers_agent() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)

    result = await _attach(service, _access(service), agent)

    assert result["ok"] is True and result["attached"] is True, result
    stored = authorities[agent.access_id]
    assert stored.grantor_subject == OWNER, "the agent's Card stays under its owner"
    assert stored.control_card.control_id == CONTROL_ID
    assert stored.control_card.holder_subject == CREATOR, "the binding records the Control Card's holder"
    audit = stored.provenance[PROJECT_CONTROL_CARD_ATTACH_AUDIT_PROVENANCE]
    assert audit["actor_subject"] == "admin-a" and audit["action"] == "attached"
    assert audit["control_holder"] == CREATOR and audit["agent_via"] == "project_admin_linking"
    assert result["control_card"]["state"] == "active"


@pytest.mark.asyncio
async def test_the_runtime_composes_under_the_holder_and_the_control_card_only_narrows() -> None:
    agent, control = _cards(control_operations=())
    service, authorities = _service(agent, control)
    assert (await _attach(service, _access(service), agent))["ok"] is True

    record = await service._load_record(agent.access_id, grantor_subject=OWNER)
    resolved, composed = await service._compose_with_control(record)

    assert resolved is not None and resolved.grantor_subject == CREATOR
    # AND: the Control Card selects no operation, so none survives on the agent.
    assert not any(composed.resource_operations.get(RESOURCE, ()))


@pytest.mark.asyncio
async def test_the_plain_attach_never_takes_another_persons_control_card() -> None:
    """Only the project path records a holder; a caller cannot name one."""

    agent, control = _cards()
    service, authorities = _service(agent, control)

    refused = await service.attach_control_card(
        {"user_id": OWNER}, access_id=agent.access_id, control_id=CONTROL_ID,
    )

    assert refused["ok"] is False
    assert refused["error"] in {"control_card_unavailable", "control_card_grantor_mismatch"}
    assert authorities[agent.access_id].control_card is None


@pytest.mark.asyncio
async def test_a_revoked_control_card_fails_every_bound_agent_closed() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    assert (await _attach(service, _access(service), agent))["ok"] is True

    authorities[CONTROL_ID] = dataclasses.replace(authorities[CONTROL_ID], state="revoked")
    record = await service._load_record(agent.access_id, grantor_subject=OWNER)

    assert await service._compose_with_control(record) == (None, None)
    view = await service._effective_control_view(record)
    assert view["state"] != "active", "no fallback to the agent's own Card"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_port", "agent_port", "error"),
    [
        (ControlPort(allowed=False), AgentPort(), "work_project_control_card_attach_denied"),
        (ControlPort(via="project_member"), AgentPort(), "project_control_card_write_denied"),
        (ControlPort(), AgentPort(allowed=False), "work_agent_card_write_denied"),
        (ControlPort(), AgentPort(via="platform_admin"), "project_agent_card_write_denied"),
    ],
)
async def test_attach_and_detach_each_need_both_decisions(control_port, agent_port, error) -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    assert (await _attach(service, _access(service), agent))["ok"] is True
    access = _access(service, control_port=control_port, agent_port=agent_port)

    for result in (
        await access.attach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id,
                            replace_control_id=CONTROL_ID),
        await access.detach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id),
    ):
        assert result["ok"] is False and result["error"] == error, result
    assert authorities[agent.access_id].control_card is not None, "detach refused: still narrowed"


@pytest.mark.asyncio
async def test_a_second_admin_detaches_through_the_project() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    access = _access(service)
    assert (await _attach(service, access, agent))["ok"] is True

    result = await access.detach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id,
                                 request_id="req-detach")

    assert result["ok"] is True and result["detached"] is True, result
    stored = authorities[agent.access_id]
    assert stored.control_card is None
    assert stored.provenance[PROJECT_CONTROL_CARD_ATTACH_AUDIT_PROVENANCE]["action"] == "detached"


@pytest.mark.asyncio
async def test_the_linking_via_changes_nothing_but_the_binding() -> None:
    """claude-main's condition: project_admin_linking attaches and detaches, never edits an agent Card."""

    agent, control = _cards()
    service, _ = _service(agent, control)
    agent_access = ProjectAgentCardAccess(service, AgentPort(via="project_admin_linking"))

    refused = await agent_access.update(
        ADMIN, access_id=agent.access_id, project_ref=PROJECT, resource_grants={RESOURCE: ["messages:read"]},
    )

    assert refused["ok"] is False and refused["reason"] == "decision_via_cannot_edit"


# -- the runtime rule, directly ------------------------------------------------


def _bound(agent, control, *, holder):
    binding = ControlCardBinding(control_id=control.access_id, issuer_ref=control.issuer_ref, holder_subject=holder)
    return dataclasses.replace(agent, control_card=binding)


def test_a_binding_without_a_holder_keeps_todays_rule() -> None:
    agent, control = _cards()
    own_control = dataclasses.replace(control, grantor_subject=OWNER)
    # The owner's own Control Card composes, as before.
    effective_card_authority(_bound(agent, own_control, holder=""), own_control)
    # Another person's Control Card without a recorded holder is refused, as before.
    with pytest.raises(ControlCardMismatch) as refused:
        effective_card_authority(_bound(agent, control, holder=""), control)
    assert refused.value.reason == "control_card_grantor_mismatch"


def test_the_holder_must_be_the_control_cards_own_grantor() -> None:
    agent, control = _cards()
    effective_card_authority(_bound(agent, control, holder=CREATOR), control)
    with pytest.raises(ControlCardMismatch) as refused:
        effective_card_authority(_bound(agent, control, holder="someone-else"), control)
    assert refused.value.reason == "control_card_grantor_mismatch"


def test_another_persons_control_card_may_only_narrow() -> None:
    agent, control = _cards()
    widening = dataclasses.replace(control, composition_mode="or")
    with pytest.raises(ControlCardMismatch) as refused:
        effective_card_authority(_bound(agent, widening, holder=CREATOR), widening)
    assert refused.value.reason == "control_card_foreign_holder_requires_and"
    assert control.composition_mode in ("", CONTROL_COMPOSITION_AND)



# -- review on app-ecosystem#192: the owner cannot drop the project's narrowing --


@pytest.mark.asyncio
async def test_the_agents_owner_cannot_detach_the_projects_control_card_on_the_plain_path() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    assert (await _attach(service, _access(service), agent))["ok"] is True

    refused = await service.detach_control_card(
        {"user_id": OWNER}, access_id=agent.access_id, control_id=CONTROL_ID,
    )

    assert refused["ok"] is False and refused["error"] == "control_card_held_by_project"
    assert refused["status"] == 409
    assert authorities[agent.access_id].control_card.holder_subject == CREATOR, "still narrowed"


@pytest.mark.asyncio
async def test_the_agents_owner_cannot_replace_the_projects_control_card_on_the_plain_path() -> None:
    agent, control = _cards()
    own = dataclasses.replace(control, access_id="control-owners-own", grantor_subject=OWNER)
    service, authorities = _service(agent, control)
    authorities[own.access_id] = own
    assert (await _attach(service, _access(service), agent))["ok"] is True

    refused = await service.attach_control_card(
        {"user_id": OWNER}, access_id=agent.access_id, control_id=own.access_id,
        replace_control_id=CONTROL_ID,
    )

    assert refused["ok"] is False and refused["error"] == "control_card_held_by_project"
    assert authorities[agent.access_id].control_card.control_id == CONTROL_ID


def test_a_holder_never_makes_a_credentialed_card_a_control_card() -> None:
    """Review P3: the credentialless check is the gate a foreign holder passes through."""

    agent, control = _cards()
    credentialed = dataclasses.replace(control, delegate_subject="someone", source="oauth")
    with pytest.raises(ControlCardMismatch) as refused:
        effective_card_authority(_bound(agent, credentialed, holder=CREATOR), credentialed)
    assert refused.value.reason == "control_card_has_credential"


# -- the agent's owner while linking or unlinking their own agent (claude-main, 2026-09-26) --


@pytest.mark.asyncio
async def test_the_owner_linking_attaches_but_never_detaches() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    linking = _access(service, control_port=ControlPort(via="owner_linking"), agent_port=AgentPort(via="owner"))

    attached = await linking.attach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id)
    assert attached["ok"] is True, attached
    assert authorities[agent.access_id].control_card.holder_subject == CREATOR

    refused = await linking.detach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id)
    assert refused == {"ok": False, "error": "project_control_card_write_denied",
                       "reason": "decision_via_cannot_bind", "status": 403}
    assert authorities[agent.access_id].control_card is not None, "still narrowed"


@pytest.mark.asyncio
async def test_the_owner_unlinking_detaches_but_never_attaches() -> None:
    agent, control = _cards()
    service, authorities = _service(agent, control)
    unlinking = _access(service, control_port=ControlPort(via="owner_unlinking"), agent_port=AgentPort(via="owner"))

    refused = await unlinking.attach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id)
    assert refused["ok"] is False and refused["reason"] == "decision_via_cannot_bind"
    assert authorities[agent.access_id].control_card is None

    assert (await _attach(service, _access(service), agent))["ok"] is True
    detached = await unlinking.detach(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, access_id=agent.access_id)
    assert detached["ok"] is True and detached["detached"] is True, detached
    assert authorities[agent.access_id].control_card is None


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["owner_linking", "owner_unlinking"])
async def test_an_owners_linking_answer_is_never_a_read_or_write(via) -> None:
    agent, control = _cards()
    service, _ = _service(agent, control)
    access = ProjectControlCardAccess(service, ControlPort(via=via))
    for call in (
        access.get(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT),
        access.update(ADMIN, control_id=CONTROL_ID, project_ref=PROJECT, label="Renamed"),
    ):
        refused = await call
        assert refused["ok"] is False and refused["reason"] == "decision_via_invalid", refused

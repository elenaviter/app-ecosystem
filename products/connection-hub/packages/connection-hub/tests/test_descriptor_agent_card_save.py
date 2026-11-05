"""Descriptor agents are recognized from stored authority, never routed as projects."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.descriptor_agent_card import (
    DescriptorAgentCardRefused, validate_descriptor_agent_control,
)
from test_agent_capability_control_sync import OWNER, PROJECT, TENANT, _service, _sync


async def _descriptor_pair():
    service, persistence = _service()
    result = await _sync(service, revision="descriptor-r1", authority=("read",), catalog=("read",))
    assert result["ok"], result
    resident = persistence.records[result["card"]["access_id"]][0]
    control = persistence.records[result["control_card"]["access_id"]][0]
    service._managed_control_scopes = ("work:project:",)
    return service, resident, control


@pytest.mark.asyncio
async def test_descriptor_owner_save_does_not_ask_project_board_to_authorize_it():
    service, resident, _ = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    calls = []

    async def project_authorize(*args):
        calls.append(args)
        return {"ok": False, "error": "work_agent_card_unknown"}

    answer = await service._forward_owner_agent_card_save(
        {"user_id": OWNER}, record=record_from_card(resident), request_id="owner-save",
        authorize_agent_card=project_authorize, changes={})
    assert answer is None
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_bound_owner_save_refuses_without_project_forwarding():
    service, resident, _ = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    resident = replace(resident, control_card=replace(resident.control_card, issuer_kind="unknown"))
    calls = []

    async def project_authorize(*args):
        calls.append(args)
        return {"ok": False, "error": "work_agent_card_unknown"}

    answer = await service._forward_owner_agent_card_save(
        {"user_id": OWNER}, record=record_from_card(resident), request_id="owner-save",
        authorize_agent_card=project_authorize, changes={})
    assert answer["error"] == "agent_card_control_issuer_unknown"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer_kind", ["application", "project"])
async def test_project_managed_agent_keeps_project_authorization(issuer_kind):
    service, resident, _ = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    service._managed_control_scopes = ("work:project:",)
    scope = "work:project:synthetic"
    service.bind_managed_card_edit({scope: SimpleNamespace(forward=lambda _: None)})
    binding = ControlCardBinding(
        control_id=control_card_id_for_issuer(issuer_kind, scope, grantor_subject=OWNER),
        issuer_kind=issuer_kind, issuer_ref=scope, control_revision=1)
    resident = replace(resident, control_card=binding)
    calls = []

    async def project_authorize(*args):
        calls.append(args)
        return {"ok": False, "error": "project_policy_refused"}

    answer = await service._forward_owner_agent_card_save(
        {"user_id": OWNER}, record=record_from_card(resident), request_id="owner-save",
        authorize_agent_card=project_authorize, changes={})
    assert answer["error"] == "project_policy_refused"
    assert calls == [(resident.access_id, scope)]


@pytest.mark.asyncio
async def test_stored_descriptor_control_is_the_owner_save_ceiling():
    _, resident, control = await _descriptor_pair()
    ceiling = validate_descriptor_agent_control(
        resident, control, tenant=TENANT, project=PROJECT, grantor_subject=OWNER)
    assert set(ceiling.capabilities["tools"]) == {"read"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "owner", "tenant", "project", "client", "delegate", "access_id", "holder", "binding_id",
    "issuer", "foreign_control", "control_issuer", "control_source", "control_marker",
    "control_evidence", "control_revision", "ended_control",
])
async def test_foreign_forged_or_stale_descriptor_binding_never_authorizes_save(case):
    _, resident, control = await _descriptor_pair()
    owner, tenant, project = OWNER, TENANT, PROJECT
    if case == "owner":
        owner = "another-user"
    elif case == "tenant":
        tenant = "another-tenant"
    elif case == "project":
        project = "another-deployment"
    elif case == "client":
        resident = replace(resident, client_id="kdcube-agent:another-app:another-agent")
    elif case == "delegate":
        resident = replace(resident, delegate_subject="integration:another")
    elif case == "access_id":
        resident = replace(resident, access_id="agent-another")
    elif case == "holder":
        resident = replace(resident, control_card=replace(resident.control_card, holder_subject="another-user"))
    elif case == "binding_id":
        resident = replace(resident, control_card=replace(resident.control_card, control_id="ctrl-another"))
    elif case == "issuer":
        resident = replace(resident, control_card=replace(resident.control_card, issuer_kind="unknown"))
    elif case == "foreign_control":
        control = replace(control, grantor_subject="another-user")
    elif case == "control_issuer":
        control = replace(control, issuer_kind="unknown")
    elif case == "control_source":
        control = replace(control, client_id="control-card:another")
    elif case == "control_marker":
        control = replace(control, properties={})
    elif case == "control_evidence":
        control = replace(control, resource_acceptance={})
    elif case == "control_revision":
        control = replace(control, card_revision=control.card_revision + 1)
    elif case == "ended_control":
        control = replace(control, state="revoked")
    with pytest.raises(DescriptorAgentCardRefused):
        validate_descriptor_agent_control(
            resident, control, tenant=tenant, project=project, grantor_subject=owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "stale", "marker", "invalid_marker", "foreign_owner", "foreign_deployment"])
async def test_bad_stored_descriptor_control_refuses_before_choosing_local_route(case):
    service, resident, control = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    persistence = service._persistence
    if case == "missing":
        persistence.records.pop(control.access_id)
    else:
        _, handles = persistence.records[control.access_id]
        changes = {
            "stale": {"card_revision": control.card_revision + 1},
            "marker": {"properties": {}},
            "invalid_marker": {"properties": {"kdcube.agent_descriptor_control": {"schema": "invalid"}}},
            "foreign_owner": {"grantor_subject": "another-user"},
            "foreign_deployment": {"issuer_ref": control.issuer_ref.replace("tenant-a", "another-tenant")},
        }
        persistence.records[control.access_id] = (replace(control, **changes[case]), handles)
    calls = []

    async def authorize(*args):
        calls.append(args)
        return {"ok": True}

    answer = await service._forward_owner_agent_card_save(
        {"user_id": OWNER}, record=record_from_card(resident), request_id="owner-save",
        authorize_agent_card=authorize, changes={})
    assert answer is not None and answer["ok"] is False
    assert answer["error"].startswith("agent_capability_")
    assert calls == []


@pytest.mark.asyncio
async def test_descriptor_control_owner_edit_still_requires_platform_admin():
    service, _, control = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    answer = await service.update_access(
        {"user_id": OWNER}, access_id=control.access_id, resource_grants=control.resource_grants,
        resource_operations=control.resource_operations, expected_card_revision=control.card_revision,
        label="Owner must not change administrator Control")
    assert answer["ok"] is False and answer["error"] == "platform_admin_required"

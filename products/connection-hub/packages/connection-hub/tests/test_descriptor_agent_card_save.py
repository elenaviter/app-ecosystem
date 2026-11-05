"""Descriptor agents are recognized from stored authority, never routed as projects."""

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from test_agent_capability_control_sync import OWNER, _service, _sync


async def _descriptor_pair():
    service, persistence = _service()
    result = await _sync(service, revision="descriptor-r1", authority=("read",), catalog=("read",))
    assert result["ok"], result
    resident = persistence.records[result["card"]["access_id"]][0]
    control = persistence.records[result["control_card"]["access_id"]][0]
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
async def test_project_managed_agent_keeps_project_authorization():
    service, resident, _ = await _descriptor_pair()
    service._card_coordinator = (None, None, None, 60)
    service._managed_control_scopes = ("work:project:",)
    scope = "work:project:synthetic"
    binding = ControlCardBinding(
        control_id=control_card_id_for_issuer("application", scope, grantor_subject=OWNER),
        issuer_kind="application", issuer_ref=scope, control_revision=1)
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

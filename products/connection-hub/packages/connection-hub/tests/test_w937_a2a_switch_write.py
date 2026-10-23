# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W937: the project's A2A switch is written only by the project's own P edit.

The same write path as the External MCP switch (W918), hop by hop: the
signed edit body, the forward from a Control Save (direct writes refused),
the lifecycle PLAN field and the planner that stores it on P. Each switch
travels on its own; a Save that carries neither is unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.controls.external_mcp_switch import (
    A2A_DIRECT_WRITE_REFUSED, A2A_FAMILY_PROPERTY, EXTERNAL_MCP_FAMILY_PROPERTY, SWITCH_PROPERTIES,
    direct_switch_write_refusal, validate_family_property,
)
from connection_hub.delegated_credentials.managed_card_edit_forward import ManagedCardEditError, managed_card_edit_body
from connection_hub.delegated_credentials.managed_card_selection_plan import (
    OPERATIONS, build_managed_card_selection_update,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision, ProjectAuthorizationRequest,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused


def _body(kind="project_control", **extra):
    return managed_card_edit_body(actor_subject="alice", project_ref="work:project:p", request_id="r-1",
        kind=kind, principal_key="card:p-1", original_revision=2, access_id="p-1", subject_hash="h",
        selection={}, **extra)


def test_the_switch_set_is_closed_and_each_is_validated() -> None:
    assert SWITCH_PROPERTIES == (EXTERNAL_MCP_FAMILY_PROPERTY, A2A_FAMILY_PROPERTY)
    validate_family_property({A2A_FAMILY_PROPERTY: "on", EXTERNAL_MCP_FAMILY_PROPERTY: "off"})
    with pytest.raises(ValueError, match="control_card_a2a_family_invalid"):
        validate_family_property({A2A_FAMILY_PROPERTY: "yes"})


def test_a_direct_write_of_the_a2a_switch_is_refused_by_its_own_code() -> None:
    refused = direct_switch_write_refusal("on", switch_property=A2A_FAMILY_PROPERTY)
    assert refused["error"] == A2A_DIRECT_WRITE_REFUSED and "A2A agents" in refused["message"]
    assert direct_switch_write_refusal(None, switch_property=A2A_FAMILY_PROPERTY) is None
    assert direct_switch_write_refusal("on")["error"] == "external_mcp_family_requires_project_transaction"


def test_the_a2a_switch_is_an_exact_field_of_a_project_control_body_only() -> None:
    assert _body(a2a_family="on")["a2a_family"] == "on"
    both = _body(a2a_family="off", external_mcp_family="on")
    assert both["a2a_family"] == "off" and both["external_mcp_family"] == "on"
    assert "a2a_family" not in _body(external_mcp_family="on")  # each travels only when set
    for bad in ("ON", "", "yes"):
        with pytest.raises(ManagedCardEditError):
            _body(a2a_family=bad)
    with pytest.raises(ManagedCardEditError):
        _body(kind="agent_card", a2a_family="on")


def _forward(svc, record, **changes):
    return asyncio.run(svc._forward_managed_project_control(
        {"user_id": "alice"}, record=record, request_id="a2a-family-1",
        changes=dict(expected_card_revision=5, **changes)))


def test_a_changed_a2a_switch_travels_signed_through_the_project() -> None:
    from test_w638_project_control_forward import Host, project_control as forwarded_p, service

    host = Host()
    result = _forward(service(host), forwarded_p(card_revision=5), a2a_family="on")
    assert result["ok"] is True, result
    (_, _, body), = host.calls
    assert body["a2a_family"] == "on" and "external_mcp_family" not in body and body["selection"] == {}
    assert "service_proof" in body


def test_an_unchanged_a2a_switch_answers_ok_without_a_write() -> None:
    from test_w638_project_control_forward import Host, project_control as forwarded_p, service

    host = Host()
    stored = forwarded_p(card_revision=5, properties={"kept": "as stored", A2A_FAMILY_PROPERTY: "on"})
    result = _forward(service(host, durable=stored), stored, a2a_family="on")
    assert result == {"ok": True, "unchanged": True} and host.calls == []


def test_two_switch_values_are_two_saves_and_a_save_without_one_keeps_its_id() -> None:
    from test_w638_project_control_forward import Host, project_control as forwarded_p, service

    def request_id(**changes):
        host = Host()
        asyncio.run(service(host)._forward_managed_project_control(
            {"user_id": "alice"}, record=forwarded_p(card_revision=5), request_id=None,
            changes=dict(expected_card_revision=5, **changes)))
        return host.calls[0][2]["request_id"] if host.calls else None

    plain = request_id(composition_mode="or")
    assert request_id(composition_mode="or", a2a_family="on") != plain
    assert request_id(composition_mode="or", a2a_family="on") != request_id(composition_mode="or", a2a_family="off")
    assert request_id(composition_mode="or") == plain


async def test_an_a2a_switch_only_screen_save_is_a_real_signed_forward() -> None:
    from connection_hub.delegated_credentials.automation_access import record_from_card
    from test_w638_managed_card_selection_plan import PROJECT, project_control as stored_p
    from test_w638_project_control_forward import Host, service
    from test_w756_whole_card_diff import Catalog

    peer = Host()
    host = service(peer)
    record = record_from_card(stored_p())

    async def current(*args, **kwargs):
        return record, "active"

    async def active():
        return SimpleNamespace(version=record.catalog_version)

    async def config(*args, **kwargs):
        return Catalog()

    host._load_record_any_state = current
    host._active_catalog = active
    host._catalog_config = config
    result = await host._forward_agent_card_edit(
        {"user_id": "alice"}, record=record, project_ref=PROJECT,
        request_id="a2a-switch-screen-save", kind="project_control",
        changes={"expected_card_revision": record.card_revision,
                 "expected_catalog_version": record.catalog_version,
                 "_screen_save": True, "a2a_family": "on"},
    )
    assert result["ok"] is True
    assert len(peer.calls) == 1
    assert peer.calls[0][2]["a2a_family"] == "on" and "external_mcp_family" not in peer.calls[0][2]


def test_the_planner_stores_the_a2a_switch_on_the_project_control_only() -> None:
    from test_w607_existing_card_selection_plan import _Host
    from test_w638_managed_card_selection_plan import PROJECT, project_control

    original = project_control()
    request = ProjectAuthorizationRequest.build(
        actor_subject="alice", project_ref=PROJECT, target_subject=original.access_id,
        operation=OPERATIONS["project_control"], request_id="a2a-save")
    decision = ProjectAuthorizationDecision.allow(request, platform_admin=True)
    built = asyncio.run(build_managed_card_selection_update(
        _Host(), original=original, kind="project_control", selection={},
        active=SimpleNamespace(version=original.catalog_version), decision=decision,
        project_ref=PROJECT, actor_subject="alice", request_id="a2a-save", now=1_800_000_001,
        a2a_family="on"))
    member = built["member"]
    candidate = member.candidate if hasattr(member, "candidate") else member["candidate"]
    properties = dict((candidate.properties if hasattr(candidate, "properties") else candidate["properties"]) or {})
    assert properties[A2A_FAMILY_PROPERTY] == "on"
    assert properties.get(EXTERNAL_MCP_FAMILY_PROPERTY) == dict(original.properties or {}).get(EXTERNAL_MCP_FAMILY_PROPERTY)

    class Refusing:
        async def _resolve_card_authority(self, **kwargs):
            pytest.fail("an invalid switch must refuse before any resolution")

    for kind, value in (("agent_card", "on"), ("project_control", "maybe")):
        with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
            asyncio.run(build_managed_card_selection_update(Refusing(), original=project_control(), kind=kind,
                selection={}, active=None, decision=None, project_ref=PROJECT, actor_subject="alice",
                request_id="plan-1", now=1_800_000_000, a2a_family=value))


def test_the_lifecycle_plan_admits_the_switch_only_on_the_project_controls_reselect() -> None:
    import inspect

    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert '"a2a_family" in raw and (raw.get("kind") != "reselect_project_control"' in source
    assert 'a2a_family=raw.get("a2a_family")' in source


def test_the_switch_is_a_classified_authorization_property() -> None:
    from connection_hub.delegated_credentials.card_property_classes import AUTHORIZATION, authorization_properties

    assert A2A_FAMILY_PROPERTY in AUTHORIZATION
    assert authorization_properties({A2A_FAMILY_PROPERTY: "on", "client_setting": 1}) == {A2A_FAMILY_PROPERTY: "on"}


@pytest.mark.parametrize(("switch", "code"), [
    ("a2a_family", "a2a_family_requires_project_transaction"),
    ("external_mcp_family", "external_mcp_family_requires_project_transaction"),
])
def test_a_direct_control_save_carrying_a_switch_is_refused_before_any_write(switch, code) -> None:
    from test_w638_project_control_forward import project_control as stored_p, service

    svc = service(managed=False)  # not a managed project: the direct Control write path
    record = stored_p()

    async def load_record(access_id, *, grantor_subject):
        return record

    async def update_access(*args, **kwargs):
        pytest.fail("a switch on a direct write must refuse before any write")

    svc._load_record = load_record
    svc.update_access = update_access
    result = asyncio.run(svc.control_card_update({"user_id": "alice"}, control_id=record.access_id, **{switch: "on"}))
    assert result["ok"] is False and result["error"] == code


def test_both_direct_write_refusals_are_registered_save_codes() -> None:
    from connection_hub.card_save_codes import CARD_SAVE_CODES

    for switch in SWITCH_PROPERTIES:
        assert direct_switch_write_refusal("on", switch_property=switch)["error"] in CARD_SAVE_CODES

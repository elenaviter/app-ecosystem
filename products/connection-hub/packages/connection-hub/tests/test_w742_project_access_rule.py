"""W742 (W730 row P6): the project access rule (AND/OR) of a managed project Control (P) is set through the project.

With Card transactions on, PB's project.control.update reached the Hub's managed P forward and was refused:
OR as managed_card_edit_fields_unsupported (409), AND (unchanged) as managed_card_edit_request_invalid (400).
The rule now travels as an exact, signed field of the project's P edit, only when it changes; a Save that
changes nothing answers ok without a write. Every body without it is byte-identical to before.
"""
from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.managed_card_edit_forward import (
    ManagedCardEditError, managed_card_edit_body,
)
from connection_hub.delegated_credentials.managed_card_selection_plan import build_managed_card_selection_update
from service_foundation.coordination.durable_decision_log import DecisionRefused
from test_w638_project_control_forward import Host, project_control, service
from test_w638_managed_card_selection_plan import PROJECT as PLAN_PROJECT, project_control as stored_p


def _p_body(**extra):
    return managed_card_edit_body(actor_subject="alice", project_ref="work:project:p", request_id="r-1",
        kind="project_control", principal_key="card:p-1", original_revision=2, access_id="p-1", subject_hash="h",
        **extra)


def test_the_rule_is_an_exact_field_of_a_project_control_body_only():
    body = _p_body(selection={}, composition_mode="or")
    assert body["composition_mode"] == "or" and body["selection"] == {}
    plain = _p_body(selection={"resource_operations": {"svc": ["a"]}})
    assert "composition_mode" not in plain  # every body without it is unchanged
    for bad in (dict(composition_mode="xor"), dict(composition_mode="")):
        with pytest.raises(ManagedCardEditError):
            _p_body(selection={}, **bad)
    with pytest.raises(ManagedCardEditError):
        managed_card_edit_body(actor_subject="alice", project_ref="work:project:p", request_id="r-1",
            kind="agent_card", principal_key="card:a-1", original_revision=2, access_id="a-1", subject_hash="h",
            selection={}, composition_mode="or")
    with pytest.raises(ManagedCardEditError):
        _p_body(selection={})  # still nothing to change


def _rule(svc, record, mode, **extra):
    changes = dict(expected_card_revision=5, composition_mode=mode, **extra)
    return asyncio.run(svc._forward_managed_project_control({"user_id": "alice"}, record=record,
                                                            request_id="rule-1", changes=changes))


@pytest.mark.parametrize("stored,mode", [("", "or"), ("and", "or"), ("or", "and")])
def test_a_changed_rule_travels_signed_through_the_project_and_nothing_else(stored, mode):
    host = Host()
    result = _rule(service(host), project_control(composition_mode=stored), mode)
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed", result
    (_, _, body), = host.calls
    assert body["composition_mode"] == mode and body["selection"] == {}
    assert body["target"]["kind"] == "project_control" and body["target"]["original_revision"] == 5
    assert "service_proof" in body


@pytest.mark.parametrize("stored,mode", [("", "and"), ("and", "and"), ("or", "or")])
def test_an_unchanged_rule_answers_ok_without_a_write(stored, mode):
    host = Host()
    result = _rule(service(host), project_control(composition_mode=stored, card_revision=5), mode)
    assert result == {"ok": True, "unchanged": True} and host.calls == []


def test_an_unchanged_rule_on_a_moved_card_is_not_reported_as_saved():
    host = Host()
    result = _rule(service(host), project_control(composition_mode="and", card_revision=6), "and")
    assert result["ok"] is False and result["error"] == "managed_card_edit_revision_changed" and host.calls == []


def test_a_rule_with_a_selection_travels_with_it():
    host = Host()
    _rule(service(host), project_control(), "or", resource_operations={"problem-board": ["project.cards.manage"]})
    (_, _, body), = host.calls
    assert body["composition_mode"] == "or"
    assert body["selection"] == {"resource_operations": {"problem-board": ["project.cards.manage"]}}


def test_a_selection_save_that_sends_the_same_rule_is_byte_identical_to_before():
    host = Host()
    _rule(service(host), project_control(composition_mode="and"), "and",
          resource_operations={"problem-board": ["project.cards.manage"]})
    (_, _, body), = host.calls
    assert "composition_mode" not in body


def test_properties_are_still_not_changed_by_a_p_edit():
    host = Host()
    result = _rule(service(host), project_control(), "or", properties={"x": 1})
    assert result["error"] == "managed_card_edit_fields_unsupported" and host.calls == []


def test_the_planner_takes_the_rule_only_for_the_project_control():
    class Host:
        async def _resolve_card_authority(self, **kwargs):
            pytest.fail("an invalid rule must refuse before any resolution")

    for kind, mode in (("agent_card", "or"), ("project_control", "xor")):
        with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
            asyncio.run(build_managed_card_selection_update(Host(), original=stored_p(), kind=kind, selection={},
                active=None, decision=None, project_ref=PLAN_PROJECT, actor_subject="alice", request_id="plan-1",
                now=1_800_000_000, composition_mode=mode))


def test_the_rule_is_compared_with_ps_durable_revision_not_a_projection_behind_it():
    host = Host()
    served = project_control(composition_mode="and", card_revision=5)  # the serving projection, one behind
    durable = project_control(composition_mode="or", card_revision=6)
    result = asyncio.run(service(host, durable=durable)._forward_managed_project_control(
        {"user_id": "alice"}, record=served, request_id="rule-2",
        changes=dict(expected_card_revision=6, composition_mode="or")))
    assert result == {"ok": True, "unchanged": True} and host.calls == []

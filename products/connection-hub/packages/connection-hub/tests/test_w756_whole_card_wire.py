"""W756 signed whole-Card carrier; this codec is not PB authorization qualification."""

import pytest

from connection_hub.delegated_credentials.managed_card_edit_forward import (
    ManagedCardEditError, managed_card_edit_body,
)


def body(kind="person_my", **changes):
    values = dict(actor_subject="alice", project_ref="work:project:synthetic", request_id="whole-save-1",
                  kind=kind, principal_key="user:alice", original_revision=29,
                  selection={"resource_grants": {"service": ["read"]},
                             "resource_operations": {"service": ["read", "write"]}, "account_scope": {}},
                  expected_catalog_version="catalog-29")
    if kind in {"agent_card", "invitation_control", "project_control"}:
        values.update(principal_key="card:agent-1", access_id="agent-1", subject_hash="a" * 64)
    values.update(changes)
    return managed_card_edit_body(**values)


@pytest.mark.parametrize("kind", ["person_my", "agent_card"])
def test_added_modes_and_render_catalog_are_exact_signed_fields(kind):
    modes = {"service": {"write": "once"}}
    result = body(kind, invocation_modes=modes)
    assert result["invocation_modes"] == modes
    assert result["expected_catalog_version"] == "catalog-29"
    assert "accepted_operations" not in result
    modes["service"]["write"] = "always"
    assert result["invocation_modes"] == {"service": {"write": "once"}}


def test_catalog_pin_travels_even_without_an_accept_update_or_modes():
    assert body()["expected_catalog_version"] == "catalog-29"


def test_own_my_properties_are_carried_in_the_same_whole_selection():
    selection = {"resource_operations": {"service": ["read", "write"]},
                 "resource_grants": {"service": ["read"]}, "account_scope": {},
                 "properties": {"kept": "unchanged-copy"}}
    result = body(selection=selection, label="Renamed Card", invocation_modes={"service": {"write": "always"}})
    assert result["selection"] == selection
    assert result["label"] == "Renamed Card"


@pytest.mark.parametrize("modes", ["once", {"service": {"write": "sometimes"}},
                                  {"service": {"write": {"mode": "once", "account_id": "untrusted"}}}])
def test_mode_carrier_is_closed_and_has_no_integration_credentials_or_scope_fields(modes):
    with pytest.raises(ManagedCardEditError):
        body(invocation_modes=modes)


@pytest.mark.parametrize("kind", ["person_control", "invitation_control", "project_control"])
def test_control_edits_never_carry_invocation_modes(kind):
    with pytest.raises(ManagedCardEditError):
        body(kind, invocation_modes={"service": {"write": "once"}})

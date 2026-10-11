"""W735 (W730 rows X2, X3): a managed Card's rename goes through the project; a Card that cannot lapse is not renewed.

X3: a label-only Save of a managed Card refused managed_card_edit_fields_unsupported. The label (a display field,
never authority) now travels as an exact, signed field of the project's managed edit, only when it changes.
X2: a Control (P, C) and a person's My Card never carry an expiry (the Card model refuses one); renewing one
answers delegated_access_managed_card_does_not_expire, not a manual/agent source refusal.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.card_lifecycle_plan import _valid_label
from connection_hub.delegated_credentials.managed_card_edit_forward import (
    LABEL_MAX, ManagedCardEditError, managed_card_edit_body,
)
from test_w638_managed_card_edit_forward import Host as AgentHost, agent_forward, service as agent_service
from test_w638_project_control_forward import Host, project_control, service


def _body(kind="person_control", **extra):
    common = dict(actor_subject="alice", project_ref="work:project:p", request_id="r-1", original_revision=2)
    if kind in {"agent_card", "project_control", "invitation_control"}:
        return managed_card_edit_body(kind=kind, principal_key="card:c-1", access_id="c-1", subject_hash="h",
                                      **common, **extra)
    principal = "user:alice" if kind == "person_my" else "user:bob"
    return managed_card_edit_body(kind=kind, principal_key=principal, **common, **extra)


@pytest.mark.parametrize("kind", ["person_control", "person_my", "agent_card", "project_control", "invitation_control"])
def test_a_rename_alone_is_an_exact_signed_field_of_every_managed_kind(kind):
    body = _body(kind, selection={}, label="Renamed")
    assert body["label"] == "Renamed" and body["selection"] == {}
    plain = _body(kind, selection={"resource_operations": {"svc": ["a"]}})
    assert "label" not in plain  # every body without a rename is unchanged


@pytest.mark.parametrize("bad", ["", " padded", "x" * (LABEL_MAX + 1), "line\nbreak", 7])
def test_an_inexact_label_is_refused_before_anything_is_sent(bad):
    with pytest.raises(ManagedCardEditError):
        _body(selection={}, label=bad)
    assert _valid_label(bad) is False
    assert _valid_label("A plain name") is True


def test_an_agent_cards_rename_travels_alone_and_an_unchanged_name_does_not():
    host = AgentHost()
    result = agent_forward(agent_service(host), resource_operations=None, label="Build agent")
    assert result["ok"] is True, result
    (_, _, body), = host.calls
    assert body["label"] == "Build agent" and body["selection"] == {}
    host = AgentHost()
    agent_forward(agent_service(host), label="Agent")  # the stored name, with a selection change
    (_, _, body), = host.calls
    assert "label" not in body


def _p_save(host, record, **changes):
    arguments = dict(expected_card_revision=5, **changes)
    return asyncio.run(service(host)._forward_managed_project_control({"user_id": "alice"}, record=record,
                                                                      request_id="p-rename", changes=arguments))


def test_the_project_controls_rename_travels_and_the_same_name_changes_nothing():
    host = Host()
    result = _p_save(host, project_control(card_revision=5), label="Maintenance P")
    assert result["ok"] is True and host.calls[0][2]["label"] == "Maintenance P"
    host = Host()
    assert _p_save(host, project_control(card_revision=5), label="Project Control") == {"ok": True, "unchanged": True}
    assert host.calls == []


def test_a_persons_control_rename_is_compared_with_the_durable_card():
    host = Host()
    svc = service(host)

    async def durable(access_id, *, grantor_subject):
        return SimpleNamespace(label="Bob", properties={}), "active"
    svc._load_record_any_state = durable

    def save(label):
        return asyncio.run(svc._forward_person_control_edit({"user_id": "alice"}, project_ref="work:project:p",
            target_subject="bob", request_id="c-rename", selection={}, expected_card_revision=4, properties=None,
            composition_mode=None, label=label))
    result = save("Bob (design)")
    assert result["ok"] is True and host.calls[-1][2]["label"] == "Bob (design)"
    assert host.calls[-1][2]["target"]["kind"] == "person_control"
    calls = len(host.calls)
    with_same = save("Bob")  # nothing else changes: still the request a Save of nothing is
    assert with_same["ok"] is False and len(host.calls) == calls


@pytest.mark.parametrize("source", ["control", "project-person"])
@pytest.mark.parametrize("mode", ["prolong", "reissue"])
def test_a_card_that_cannot_lapse_is_not_renewed_by_name(source, mode):
    svc = AutomationAccessService.__new__(AutomationAccessService)
    record = SimpleNamespace(access_id="c-1", grantor_subject="alice", source=source, expires_at=0)

    async def load(access_id, *, grantor_subject):
        return record, "active"
    svc._load_record_any_state = load
    result = asyncio.run(svc.renew_access({"user_id": "alice"}, access_id="c-1", mode=mode))
    assert result["ok"] is False and result["error"] == "delegated_access_managed_card_does_not_expire", result
    assert result["status"] == 409 and result["source"] == source


def test_a_manual_cards_prolong_keeps_its_own_refusal():
    svc = AutomationAccessService.__new__(AutomationAccessService)
    record = SimpleNamespace(access_id="m-1", grantor_subject="alice", source="manual", expires_at=10, created_at=1)

    async def load(access_id, *, grantor_subject):
        return record, "active"
    svc._load_record_any_state = load
    svc._store = None
    result = asyncio.run(svc.renew_access({"user_id": "alice"}, access_id="m-1", mode="prolong"))
    assert result["error"] == "delegated_access_prolong_unsupported"

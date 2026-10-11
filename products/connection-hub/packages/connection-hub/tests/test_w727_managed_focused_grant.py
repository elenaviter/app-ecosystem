"""W727: the Cards screen's focused grant ("add permission") on a project-managed Card.

Live W705, 11 October 2026 13:46:33Z: after a committed My Card Save, the screen's grant for the newly ticked
operation reached extend_client_access's direct write and was refused card_transactions_direct_write_refused.
The grant's merged selection now goes through the project's transaction (W724 person_my, W638 agent_card), against
the revision the screen holds. The project host's decision is proven by Problem Board's own tests
(test_w727_managed_focused_grant_postgres); here the host is the W638 recording stand-in.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials import automation_access as automation_access_module
from connection_hub.delegated_credentials.automation_access import AutomationAccessRecord, NamedServiceSelection
from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity

from test_w638_managed_card_edit_forward import PROJECT, Host, service

RESOURCE = "svc"


def _card(*, control=False, operations=("a",)):
    return AutomationAccessRecord(
        access_id="card-1", label="Card", client_id="kdcube-agent:pb:me", grantor_subject="alice",
        delegate_subject="agent", card_kind="agent", operations=(), resource_grants={RESOURCE: ("use",)},
        resource_operations={RESOURCE: tuple(operations)}, named_service_operations=NamedServiceSelection.none(),
        card_revision=7,
        control_card=ControlCardBinding(control_id="p-1", issuer_ref=PROJECT) if control else None)


def _grant(svc, record, *, revision=7, authorize=None, added="b", user=None):
    candidate = replace(record, resource_operations={RESOURCE: (*record.resource_operations[RESOURCE], added)})
    return asyncio.run(svc._forward_managed_grant(
        user or {"user_id": "alice"}, record=record, candidate=candidate, expected_card_revision=revision,
        request_id=None, label=None, authorize_agent_card=authorize))


def _on(host):
    svc = service(host)
    svc._card_coordinator = object()  # Card transactions enabled (W578)

    async def committed(access_id, *, grantor_subject):
        return replace(_card(), card_revision=4), "active"  # the Card at the stand-in project's committed revision

    async def notified(*_args, **_kwargs):
        return None

    svc._load_record_any_state, svc.notify_change = committed, notified
    return svc


def _my(monkeypatch, person="alice"):
    monkeypatch.setattr(automation_access_module, "card_authority_from_record", lambda record: record)
    monkeypatch.setattr(ProjectPersonCardIdentity, "from_my_card", classmethod(
        lambda cls, authority: type("Identity", (), {"project_ref": PROJECT, "person_subject": person})()))


async def _allowed(access_id, project_ref):
    from connection_hub.delegated_credentials.project_agent_card_access import AgentCardDecision
    return AgentCardDecision(allowed=True, via="owner", grantor_subject="alice", access_id=access_id,
                             project_ref=project_ref, action="write")


def test_a_grant_on_ones_own_my_card_sends_its_whole_selection_to_the_project(monkeypatch):
    _my(monkeypatch)
    host = Host()
    result = _grant(_on(host), _card())
    assert result["ok"] is True and result["access_id"] == "card-1"
    assert result["access"]["card_revision"] == result["managed_card_edit"]["card_revision"] == 4
    (_, _, body), = host.calls
    assert body["target"] == {"kind": "person_my", "principal_key": "user:alice", "original_revision": 7}
    assert body["selection"]["resource_operations"] == {RESOURCE: ["a", "b"]}  # the Card's selection plus the addition
    assert body["selection"]["resource_grants"] == {RESOURCE: ["use"]}
    assert body["request_id"].startswith("card-grant:")


def test_a_grant_on_a_bound_agent_card_asks_the_project_policy_then_sends_an_agent_card_edit():
    host = Host()
    asked = []

    async def authorize(access_id, project_ref):
        asked.append((access_id, project_ref))
        return await _allowed(access_id, project_ref)

    result = _grant(_on(host), _card(control=True), authorize=authorize)
    assert result["ok"] is True
    assert asked == [("card-1", PROJECT)]  # the bound project, from the Card's own binding
    (_, _, body), = host.calls
    assert body["target"]["kind"] == "agent_card" and body["target"]["original_revision"] == 7
    assert body["selection"]["resource_operations"] == {RESOURCE: ["a", "b"]}


def test_the_project_policys_refusal_is_the_answer_and_nothing_is_sent():
    host = Host()

    async def deny(access_id, project_ref):
        return {"ok": False, "error": "project_agent_card_denied", "status": 403}

    assert _grant(_on(host), _card(control=True), authorize=deny) == {
        "ok": False, "error": "project_agent_card_denied", "status": 403}
    assert host.calls == []


@pytest.mark.parametrize("kind", ["my", "agent"])
def test_a_managed_grant_without_a_revision_is_refused_and_nothing_is_sent(monkeypatch, kind):
    if kind == "my":
        _my(monkeypatch)
    host = Host()
    result = _grant(_on(host), _card(control=kind == "agent"), revision=None, authorize=_allowed)
    assert (result["ok"], result["error"]) == (False, "managed_card_edit_revision_required")
    assert host.calls == []


@pytest.mark.parametrize("state", ["aborted", "pending"])
def test_an_uncommitted_project_answer_is_a_refusal_with_no_card(monkeypatch, state):
    _my(monkeypatch)
    result = _grant(_on(Host(state=state)), _card())
    assert result["ok"] is False and result["error"] == "managed_card_edit_" + state
    assert "access" not in result


@pytest.mark.parametrize("case", ["transactions_off", "not_managed", "someone_elses_my", "agent_without_policy",
                                  "no_forwarder"])
def test_the_grant_keeps_its_direct_path_when_no_managed_route_applies(monkeypatch, case):
    """None: the caller writes directly as before, and W578 still refuses a direct write to a managed Card."""
    if case in ("someone_elses_my", "no_forwarder"):
        _my(monkeypatch, person="bob" if case == "someone_elses_my" else "alice")
    host = Host()
    svc = service(None if case == "no_forwarder" else host)
    if case != "transactions_off":
        svc._card_coordinator = object()
    record = _card(control=case in ("agent_without_policy", "transactions_off"))
    assert _grant(svc, record) is None
    assert host.calls == []


def test_the_merge_base_is_the_durable_card_when_the_serving_projection_is_behind():
    """Live 10 Oct 22:49Z: the projection can trail a project commit; a grant right after a Save merges into it."""
    served, durable = _card(control=True), replace(_card(control=True, operations=("a", "z")), card_revision=8)
    svc = _on(Host())

    async def durable_current(access_id, *, grantor_subject):
        return durable, "active"

    svc._load_record_any_state = durable_current
    assert asyncio.run(svc._managed_grant_base(served)) is durable
    plain = _card()
    assert asyncio.run(svc._managed_grant_base(plain)) is plain  # not managed: never re-read


# -- non-managed Cards: the grant's direct write is exactly what it was ------------------------------------------

@pytest.mark.asyncio
async def test_a_non_managed_cards_grant_writes_the_same_card_with_or_without_a_revision(tmp_path, monkeypatch):
    from test_oauth_card_extension import CLIENT, _issued
    from test_resident_profile_cards import MEMORIES, USER, _Harness

    monkeypatch.setattr(automation_access_module.time, "time", lambda: 1_790_000_000)
    written = []
    for revision in (None, 1):
        harness = _Harness(tmp_path / f"r{revision}")
        record = await _issued(harness)
        result = await harness.service.extend_client_access(
            USER, client_id=CLIENT, access_id=record.access_id, resource=MEMORIES, claims=["memories:write"],
            expected_card_revision=revision)
        assert result["ok"] is True, result
        written.append(harness.persistence.cards[record.access_id][0].to_dict())
    assert written[0] == written[1]

"""W727: the owner's whole Save of their own project-linked agent Card goes through the project's transaction.

Live 14 Oct 2026 10:35:47Z: the operator saved the Problem Board permissions of their own agent Card, bound AND to a
project's Control Card. The Cards screen sends an owner's Save as delegated_access_update (the project path is only
for another person's agent), and update_access forwarded only a managed P (W638) and the caller's own My Card
(W724), so the Save reached the direct write and W578 refused it (card_transactions_direct_write_refused, 403).
Like the focused grant (_forward_managed_grant), the bound project now decides through its policy and the Save goes
as one managed agent_card edit. The host is the W638 recording stand-in; the project's decision is proven by
Problem Board's own tests; the entrypoint's wiring is in the bundle's test_delegated_access_create.
"""
from __future__ import annotations

import asyncio

import pytest

from test_w638_managed_card_edit_forward import PROJECT, Host, service
from test_w727_managed_focused_grant import RESOURCE, _allowed, _card


def _service(host=None, *, transactions=True, record=None):
    svc = service(host)
    if transactions:
        svc._card_coordinator = object()  # Card transactions enabled (W578)
    stored = record if record is not None else _card(control=True)

    async def load_record(access_id, *, grantor_subject):
        return stored

    svc._load_record = load_record
    return svc


def _save(svc, *, authorize=_allowed, operations=("a", "b"), request_id=None, revision=7, **extra):
    return asyncio.run(svc.update_access(
        {"user_id": "alice"}, access_id="card-1", resource_grants={RESOURCE: ["use"]},
        resource_operations={RESOURCE: list(operations)}, expected_card_revision=revision, request_id=request_id,
        _screen_save=False, _authorize_agent_card=authorize, **extra))


def test_the_owners_save_of_a_bound_agent_card_is_the_projects_agent_card_edit():
    host = Host()
    asked = []

    async def authorize(access_id, project_ref):
        asked.append((access_id, project_ref))
        return await _allowed(access_id, project_ref)

    result = _save(_service(host), authorize=authorize)
    assert result["ok"] is True, result
    assert asked == [("card-1", PROJECT)]  # the project bound in the Card's own control_card
    (_, operation, body), = host.calls
    assert body["target"]["kind"] == "agent_card" and body["target"]["access_id"] == "card-1"
    assert body["target"]["original_revision"] == 7
    assert body["selection"]["resource_operations"] == {RESOURCE: ["a", "b"]}
    assert body["actor_subject"] == "alice"
    assert body["request_id"].startswith("agent-card-save:")


def test_the_projects_refusal_is_the_answer_and_nothing_is_sent():
    host = Host()

    async def deny(access_id, project_ref):
        return {"ok": False, "error": "project_agent_card_write_denied", "status": 403}

    assert _save(_service(host), authorize=deny) == {
        "ok": False, "error": "project_agent_card_write_denied", "status": 403}
    assert host.calls == []


def test_a_retried_save_replays_one_decision_and_another_selection_is_a_new_one():
    host = Host()
    svc = _service(host)
    _save(svc)
    _save(svc)
    _save(svc, operations=("a",))
    first, again, other = (body["request_id"] for _, _, body in host.calls)
    assert first == again and other != first
    _save(svc, request_id="screen-save-1")
    assert host.calls[-1][2]["request_id"] == "screen-save-1"  # a request id the route sends is kept


@pytest.mark.parametrize("state", ["aborted", "pending"])
def test_an_uncommitted_project_answer_is_a_refusal(state):
    result = _save(_service(Host(state=state)))
    assert result["ok"] is False and result["error"] == "managed_card_edit_" + state


@pytest.mark.parametrize("case", ["transactions_off", "unbound_card", "no_project_policy"])
def test_the_forward_does_not_apply_and_the_save_keeps_its_path(case):
    """None from the forward: the Save continues as before (and W578 still refuses a direct managed write)."""
    host = Host()
    record = _card(control=case != "unbound_card")
    svc = _service(host, transactions=case != "transactions_off", record=record)
    forwarded = asyncio.run(svc._forward_owner_agent_card_save(
        {"user_id": "alice"}, record=record, request_id=None,
        authorize_agent_card=None if case == "no_project_policy" else _allowed, changes={}))
    assert forwarded is None and host.calls == []


def test_a_project_admins_fallback_write_is_not_forwarded_again():
    """ProjectAgentCardAccess.update already tried the forward for the admin; its fallback to update_access (under
    the owner's storage identity, with the admin as _caller_actor_subject) must not forward again."""
    host = Host()
    svc = _service(host)
    sent = []

    async def direct_path(*_args, **_kwargs):
        sent.append("forward")
        return {"ok": True}

    svc._forward_owner_agent_card_save = direct_path
    try:
        _save(svc, _caller_actor_subject="bob")
    except Exception:  # noqa: BLE001 - the stand-in has no direct-write machinery; only the forward matters
        pass
    assert sent == []

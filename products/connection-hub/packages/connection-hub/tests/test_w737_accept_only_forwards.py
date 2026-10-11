"""W737: an Accept-only Save forwards no selection change, and a managed P is saved by the acting person.

U3/U4: an omitted ``resource_grants`` stays omitted on its way to the project's transaction (the Hub's
planner keeps every field not sent), so an acceptance alone no longer arrives as "remove every grant".
U5: the project path saves a managed P as the authenticated admin (``_actor_user``), never as the
Card's storage owner. The project host is the recording stand-in of the W638 forward tests; PB's
decision and the end-to-end rows are proven by Applications' test_w730_w734_accept_postgres.
"""
from __future__ import annotations

import asyncio

from test_w638_project_control_forward import CREATOR, Host, project_control, service

ACCEPTED = {"problem-board": ["project.cards.manage"]}
ADMIN = "synthetic-project-admin"


def _p_service(host):
    svc = service(host)
    record = project_control(card_revision=5)

    async def load_record(access_id, *, grantor_subject):
        return record
    svc._load_record = load_record
    return svc, record


def test_a_managed_p_saved_on_the_project_path_is_signed_by_the_acting_admin():
    host = Host()
    svc, record = _p_service(host)
    result = asyncio.run(svc.control_card_update({"user_id": CREATOR}, control_id=record.access_id,
        accepted_operations=ACCEPTED, expected_card_revision=5, request_id="route-accept-p",
        _actor_user={"user_id": ADMIN}))
    assert result["ok"] is True, result
    body = host.calls[0][2]
    assert body["actor_subject"] == ADMIN  # never the storage owner
    assert body["selection"] == {} and body["accepted_operations"] == ACCEPTED


def test_without_an_acting_user_the_caller_still_signs_its_own_p_save():
    host = Host()
    svc, record = _p_service(host)
    asyncio.run(svc.control_card_update({"user_id": CREATOR}, control_id=record.access_id,
        accepted_operations=ACCEPTED, expected_card_revision=5, request_id="route-accept-own"))
    assert host.calls[0][2]["actor_subject"] == CREATOR


def test_an_omitted_selection_is_not_forwarded_as_an_empty_one():
    host = Host()
    svc, record = _p_service(host)
    result = asyncio.run(svc.update_access({"user_id": CREATOR}, access_id=record.access_id, resource_grants=None,
        accepted_operations=ACCEPTED, expected_card_revision=5, request_id="route-accept-only"))
    assert result["ok"] is True, result
    body = host.calls[0][2]
    assert "resource_grants" not in body["selection"] and body["accepted_operations"] == ACCEPTED


def test_an_explicit_selection_is_still_forwarded_exactly():
    host = Host()
    svc, record = _p_service(host)
    asyncio.run(svc.update_access({"user_id": CREATOR}, access_id=record.access_id,
        resource_grants={"problem-board": ["work:admin"]}, expected_card_revision=5, request_id="route-save"))
    assert host.calls[0][2]["selection"]["resource_grants"] == {"problem-board": ["work:admin"]}

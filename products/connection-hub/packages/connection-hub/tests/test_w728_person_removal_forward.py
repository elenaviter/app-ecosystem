"""W728: with Card transactions on, an admin's revoke of a LIVE person's project Control is forwarded once as
the project's removal of that person (managed-card-edit.v1, target ``person_removal``). Never a direct write;
no forwarder, or a pending invitation, keeps W578's refusal.
"""
from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.managed_card_edit_forward import (
    OPERATION, ManagedCardEditError, managed_card_edit_body,
)
from test_w638_managed_card_edit_forward import PROJECT, Host, service
from test_w723_w724_managed_accept_and_my_card import _verified


class StoredControl:
    card_revision = 7
    properties = {}


def _transactions_on(svc, *, revision=7):
    svc._card_coordinator = object()  # W578: Card transactions enabled

    async def load_record_any_state(access_id, *, grantor_subject):
        stored = StoredControl()
        stored.card_revision = revision
        return stored, "active"
    svc._load_record_any_state = load_record_any_state
    return svc


def _revoke(svc, **extra):
    arguments = dict(project_ref=PROJECT, target_subject="bob", request_id="audit-1")
    arguments.update(extra)
    return asyncio.run(svc.project_person_control_revoke({"user_id": "alice"}, **arguments))


def test_a_live_persons_revoke_forwards_one_signed_removal_fenced_on_the_control_revision():
    host = Host()
    result = _revoke(_transactions_on(service(host)))
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed", result
    (bundle_id, operation, body), = host.calls
    assert (bundle_id, operation) == ("problem-board@1-0", OPERATION)
    assert _verified(body) is True
    assert body["target"] == {"kind": "person_removal", "principal_key": "user:bob", "original_revision": 7}
    assert body["selection"] == {} and body["actor_subject"] == "alice" and body["project_ref"] == PROJECT
    assert body["request_id"].startswith("person-removal:") and "accepted_operations" not in body


def test_the_request_id_is_stable_for_a_retry_and_moves_with_the_revision():
    host = Host()
    _revoke(_transactions_on(service(host)))
    _revoke(_transactions_on(service(host)), request_id="audit-2")  # another audit id, the same removal
    _revoke(_transactions_on(service(host), revision=8))
    ids = [body["request_id"] for _, _, body in host.calls]
    assert ids[0] == ids[1] != ids[2]


def test_an_uncommitted_project_answer_is_returned_and_nothing_is_written_directly():
    host = Host(state="aborted")
    result = _revoke(_transactions_on(service(host)))
    assert result["ok"] is False and result["error"] == "managed_card_edit_aborted", result
    assert len(host.calls) == 1


def test_without_a_forwarder_w578_still_refuses_and_nothing_is_sent():
    svc = _transactions_on(service(None))
    result = _revoke(svc)
    assert result["error"] == "card_transactions_direct_write_refused", result


def test_a_pending_invitation_revoke_is_not_routed():
    host = Host()
    result = _revoke(_transactions_on(service(host)), invitation_ref="inv-1")
    assert result["error"] == "card_transactions_direct_write_refused" and host.calls == [], result


def test_a_person_without_a_current_control_is_not_forwarded():
    host = Host()
    svc = _transactions_on(service(host))

    async def absent(access_id, *, grantor_subject):
        return None
    svc._load_record_any_state = absent
    result = _revoke(svc)
    assert result["error"] == "card_transactions_direct_write_refused" and host.calls == [], result


@pytest.mark.parametrize("case", ["self", "selection", "acceptance", "revision", "not_a_person"])
def test_the_removal_body_is_refused_unless_exact(case):
    arguments = dict(actor_subject="alice", project_ref=PROJECT, request_id="r-1", kind="person_removal",
                     principal_key="user:bob", original_revision=3, selection={})
    if case == "self":
        arguments["principal_key"] = "user:alice"
    elif case == "selection":
        arguments["selection"] = {"resource_operations": {"svc": ["a"]}}
    elif case == "acceptance":
        arguments["accepted_operations"] = {"svc": ["a"]}
    elif case == "revision":
        arguments["original_revision"] = 0
    else:
        arguments["principal_key"] = "card:bob"
    with pytest.raises(ManagedCardEditError):
        managed_card_edit_body(**arguments)
    good = dict(arguments, principal_key="user:bob", selection={}, original_revision=3)
    good.pop("accepted_operations", None)
    assert managed_card_edit_body(**good)["target"]["kind"] == "person_removal"

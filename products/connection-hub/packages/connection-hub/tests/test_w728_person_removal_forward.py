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


def _transactions_on(svc, *, revision=7, state="active"):
    svc._card_coordinator = object()  # W578: Card transactions enabled

    async def load_record_any_state(access_id, *, grantor_subject):
        stored = StoredControl()
        stored.card_revision = revision
        return stored, state
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


class Project(Host):
    """The project as W728 decides: a request id its removal audit row holds replays; an ended member refuses."""

    def __init__(self, decided=(), member=False):
        super().__init__()
        self.decided, self.member = set(decided), member

    async def call(self, *, bundle_id, operation, data):
        body = data["data"]
        if self.member or body["request_id"] in self.decided:
            return await super().call(bundle_id=bundle_id, operation=operation, data=data)
        self.calls.append((bundle_id, operation, body))
        return {"status": "ok", "bundle_id": bundle_id, operation: {"ok": False, "status": 409, "error": {
            "code": "work_card_transaction_removal_target_invalid", "message": "Not a removable person."}}}


def test_a_lost_answer_retry_after_the_removal_replays_the_first_request():
    """Infra's W728 review: the project's removal revoked the Control (7 -> 8). The removal at 8 is refused
    (the person is gone); the first request, at 7, is then sent again and the project replays it."""
    host = Host()
    _revoke(_transactions_on(service(host)))
    (_, _, first), = host.calls
    project = Project(decided={first["request_id"]})
    result = _revoke(_transactions_on(service(project), revision=8, state="revoked"))
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed", result
    at_8, again = (body for _, _, body in project.calls)
    assert at_8["target"]["original_revision"] == 8 and at_8["request_id"] != first["request_id"]
    assert again["request_id"] == first["request_id"] and again["target"] == first["target"]


def test_a_member_whose_control_was_revoked_otherwise_is_removed_at_the_revision_read():
    project = Project(member=True)
    result = _revoke(_transactions_on(service(project), revision=8, state="revoked"))
    assert result["ok"] is True, result
    (_, _, body), = project.calls
    assert body["target"]["original_revision"] == 8  # never the reconstructed 7


def test_a_revoked_control_with_no_recorded_removal_keeps_the_projects_refusal():
    project = Project()
    result = _revoke(_transactions_on(service(project), revision=8, state="revoked"))
    assert result["ok"] is False and result["error"] == "work_card_transaction_removal_target_invalid", result
    assert [body["target"]["original_revision"] for _, _, body in project.calls] == [8, 7]


def test_a_re_invited_persons_removal_is_a_new_decision_and_another_admin_never_replays_it():
    host = Host()
    _revoke(_transactions_on(service(host)))
    _revoke(_transactions_on(service(host), revision=9))  # re-invited: an active Control again, later revision
    (_, _, first), (_, _, again) = host.calls
    assert again["request_id"] != first["request_id"] and again["target"]["original_revision"] == 9
    project = Project(decided={first["request_id"]})
    result = asyncio.run(_transactions_on(service(project), revision=8, state="revoked").project_person_control_revoke(
        {"user_id": "carol"}, project_ref=PROJECT, target_subject="bob", request_id="audit-3"))
    assert result["ok"] is False, result  # carol's ids are hers: alice's recorded removal is never replayed
    assert all(body["actor_subject"] == "carol" and body["request_id"] != first["request_id"]
               for _, _, body in project.calls)


def test_an_active_control_is_never_sent_at_an_earlier_revision():
    project = Project()
    result = _revoke(_transactions_on(service(project), revision=8))
    assert result["ok"] is False and [body["target"]["original_revision"] for _, _, body in project.calls] == [8]


def test_a_revoked_control_at_its_first_revision_is_sent_once():
    project = Project()
    result = _revoke(_transactions_on(service(project), revision=1, state="revoked"))
    assert result["error"] == "work_card_transaction_removal_target_invalid" and len(project.calls) == 1, result


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

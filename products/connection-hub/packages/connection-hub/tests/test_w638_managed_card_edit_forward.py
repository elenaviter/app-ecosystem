"""W638: a managed person-Control edit is forwarded to its project host, never written directly.

The Hub sends only what it authenticated and what the person changed, signs it
under its own protocol, and reports the host's outcome. The host's decision is
proven by the host's own tests; here the host is a recording stand-in.
"""
from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.admission import AdmissionRequest, ServiceProof, verify_admission_request
from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.managed_card_edit_forward import (
    OPERATION, OUTCOME_SCHEMA, PROTOCOL, ManagedCardEditError, PeerManagedCardEdit, managed_card_edit_body,
)
from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

SECRET = "synthetic-managed-card-edit-key-32-bytes-or-more"
PROJECT = "work:project:synthetic"


class Host:
    def __init__(self, state="committed", fail=False):
        self.state, self.fail, self.calls = state, fail, []

    async def call(self, *, bundle_id, operation, data):
        self.calls.append((bundle_id, operation, data))
        if self.fail:
            raise TimeoutError("synthetic lost answer")
        return {"ok": True, "outcome": {"schema": OUTCOME_SCHEMA, "request_id": data["request_id"],
                                        "state": self.state, "transaction_id": "tx-1", "card_revision": 4}}


class Stored:
    properties = {"kept": "as stored"}


def service(host=None):
    value = AutomationAccessService.__new__(AutomationAccessService)

    async def load_record(access_id, *, grantor_subject):
        return Stored()
    value._load_record = load_record
    if host is not None:
        value.bind_managed_card_edit({"work:project:": PeerManagedCardEdit(
            call=host.call, bundle_id="problem-board@1-0", signer_id="synthetic-hub", secret=SECRET)})
    return value


def forward(svc, **changes):
    arguments = dict(project_ref=PROJECT, target_subject="bob", request_id="edit-1",
        selection={"resource_operations": {"svc": ["a"]}}, expected_card_revision=3,
        properties=None, composition_mode=None)
    arguments.update(changes)
    return asyncio.run(svc._forward_person_control_edit({"user_id": "alice"}, **arguments))


def test_the_forward_carries_only_the_authenticated_person_and_the_changed_fields_and_verifies():
    host = Host()
    result = forward(service(host))
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed"
    (bundle_id, operation, data), = host.calls
    assert (bundle_id, operation) == ("problem-board@1-0", OPERATION)
    proof = data.pop("service_proof")
    assert data == {"schema": "managed-card-edit.v1", "actor_subject": "alice", "project_ref": PROJECT,
        "request_id": "edit-1", "target": {"kind": "person_control", "principal_key": "user:bob",
                                           "original_revision": 3},
        "selection": {"resource_operations": {"svc": ["a"]}}}
    verified = verify_admission_request(secret=SECRET, proof=ServiceProof(**proof),
        delegated_token=f"{PROTOCOL}:edit-1",
        request=AdmissionRequest(resource="problem-board@1-0", operation=OPERATION, invocation_id="edit-1",
                                 request_digest=sha256_hex(canonical_json_bytes(data)),
                                 approval_context={"protocol": PROTOCOL}), now=int(proof["timestamp"]))
    assert verified.allowed is True


@pytest.mark.parametrize("case", ["no_forwarder", "invitation", "properties", "composition", "no_revision",
                                  "empty_selection", "named_services_text"])
def test_anything_the_host_cannot_bind_exactly_is_refused_or_left_to_the_direct_refusal(case):
    host = Host()
    svc = service(None if case == "no_forwarder" else host)
    changes = {"invitation": dict(target_subject=""), "properties": dict(properties={"x": 1}),
        "composition": dict(composition_mode="or"), "no_revision": dict(expected_card_revision=None),
        "empty_selection": dict(selection={}),
        "named_services_text": dict(selection={"named_service_operations": "all"})}.get(case, {})
    result = forward(svc, **changes)
    if case in {"no_forwarder", "invitation"}:
        assert result is None  # the writer keeps card_transactions_direct_write_refused
    else:
        assert result["ok"] is False
    assert host.calls == []


def test_the_cards_unchanged_properties_sent_back_with_save_still_forward():
    host = Host()
    result = forward(service(host), properties={"kept": "as stored"})
    assert result["ok"] is True and len(host.calls) == 1
    assert "properties" not in host.calls[0][2]


@pytest.mark.parametrize("state", ["aborted", "pending"])
def test_a_terminal_refusal_or_pending_outcome_keeps_the_draft(state):
    result = forward(service(Host(state)))
    assert result["ok"] is False and result["error"] == "managed_card_edit_" + state
    assert result["managed_card_edit"]["state"] == state


def test_a_lost_answer_is_an_unknown_outcome_to_retry_with_the_same_request():
    result = forward(service(Host(fail=True)))
    assert result == {"ok": False, "error": "managed_card_edit_outcome_unknown", "status": 503,
                      "message": "The project did not answer. Retry the same change."}


def test_a_host_answer_for_another_request_is_not_accepted():
    async def call(**kwargs):
        return {"ok": True, "outcome": {"schema": OUTCOME_SCHEMA, "request_id": "other", "state": "committed",
                                        "transaction_id": "tx", "card_revision": 4}}
    peer = PeerManagedCardEdit(call=call, bundle_id="problem-board@1-0", signer_id="hub", secret=SECRET)
    body = managed_card_edit_body(actor_subject="alice", project_ref=PROJECT, request_id="edit-1",
        kind="person_control", principal_key="user:bob", original_revision=3,
        selection={"resource_operations": {"svc": []}})
    with pytest.raises(ManagedCardEditError) as refused:
        asyncio.run(peer.forward(body))
    assert refused.value.reason == "managed_card_edit_answer_invalid"

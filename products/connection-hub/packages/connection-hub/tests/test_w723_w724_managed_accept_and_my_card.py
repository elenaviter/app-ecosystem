"""W723 / W724: the Hub's managed Card edit carries "Accept update", and a person's own My Card Save has a route.

Live, 11 October 2026 (W705). A person-Control Save dropped the ticked "Accept update" decisions, and a
person's own My Card Save was refused with card_transactions_direct_write_refused because no project route
existed for it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials import automation_access as automation_access_module
from connection_hub.delegated_credentials.admission import AdmissionRequest, ServiceProof, verify_admission_request
from connection_hub.delegated_credentials.managed_card_edit_forward import (
    OPERATION, PROTOCOL, ManagedCardEditError, managed_card_edit_body,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity
from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from test_w638_managed_card_edit_forward import PROJECT, SECRET, Host, forward, service


def _verified(body):
    data = dict(body)
    proof = data.pop("service_proof")
    return verify_admission_request(secret=SECRET, proof=ServiceProof(**proof),
        delegated_token=f"{PROTOCOL}:{data['request_id']}",
        request=AdmissionRequest(resource="problem-board@1-0", operation=OPERATION,
                                 invocation_id=data["request_id"],
                                 request_digest=sha256_hex(canonical_json_bytes(data)),
                                 approval_context={"protocol": PROTOCOL}), now=int(proof["timestamp"])).allowed


def test_the_accept_update_decisions_travel_canonical_and_signed():
    host = Host()
    result = forward(service(host), accepted_operations={"svc": ["b", "a", "b"]})
    assert result["ok"] is True
    (_, _, body), = host.calls
    assert body["accepted_operations"] == {"svc": ["a", "b"]}
    assert _verified(body) is True


def test_a_save_without_acceptance_sends_exactly_the_old_body():
    host = Host()
    forward(service(host), accepted_operations=None)
    forward(service(host), accepted_operations={})
    assert all("accepted_operations" not in body for _, _, body in host.calls)


def test_an_acceptance_alone_is_a_real_edit_and_forwards():
    """The operator's second Save: only "Accept update" was ticked."""
    host = Host()
    result = forward(service(host), selection={}, accepted_operations={"svc": ["a"]})
    assert result["ok"] is True
    (_, _, body), = host.calls
    assert body["selection"] == {} and body["accepted_operations"] == {"svc": ["a"]}


@pytest.mark.parametrize("accepted", [{"svc": []}, {"svc": "a"}, {"": ["a"]}, {"svc": [""]}, {"svc": [1]}, ["svc"]])
def test_a_malformed_acceptance_is_refused_before_anything_is_sent(accepted):
    host = Host()
    result = forward(service(host), accepted_operations=accepted)
    assert result["ok"] is False and result["error"] == "managed_card_edit_request_invalid"
    assert host.calls == []


def test_the_body_names_only_ones_own_my_card():
    body = managed_card_edit_body(actor_subject="alice", project_ref=PROJECT, request_id="r-1", kind="person_my",
                                  principal_key="user:alice", original_revision=2,
                                  selection={"resource_operations": {"svc": ["a"]}})
    assert body["target"] == {"kind": "person_my", "principal_key": "user:alice", "original_revision": 2}
    with pytest.raises(ManagedCardEditError):
        managed_card_edit_body(actor_subject="alice", project_ref=PROJECT, request_id="r-1", kind="person_my",
                               principal_key="user:bob", original_revision=2,
                               selection={"resource_operations": {"svc": ["a"]}})


def test_the_person_control_save_route_passes_the_acceptance(monkeypatch):
    """project_person_control_update's managed branch: the live W705 drop."""
    host = Host()
    svc = service(host)
    svc._card_coordinator = object()  # Card transactions enabled (W578)

    async def current(user, result, **_):
        return result
    svc._with_current_person_control = current
    result = asyncio.run(svc.project_person_control_update(
        {"user_id": "alice"}, project_ref=PROJECT, target_subject="bob", request_id="edit-7",
        resource_operations={"svc": ["a"]}, expected_card_revision=3, accepted_operations={"svc": ["a"]}))
    assert result["ok"] is True
    (_, _, body), = host.calls
    assert body["accepted_operations"] == {"svc": ["a"]}


class _MyCard:
    access_id = "person-my-card-synthetic"
    grantor_subject = "alice"
    properties = {"kept": True}
    composition_mode = "and"
    label = "My Card"


def _my_route(monkeypatch, svc, *, person="alice", **changes):
    monkeypatch.setattr(automation_access_module, "card_authority_from_record", lambda record: record)
    monkeypatch.setattr(ProjectPersonCardIdentity, "from_my_card",
                        classmethod(lambda cls, authority: SimpleNamespace(project_ref=PROJECT, person_subject=person)))
    arguments = dict(record=_MyCard(), request_id="my-1", expected_card_revision=5,
                     selection={"resource_operations": {"svc": ["a", "b"]}}, properties=None, composition_mode=None,
                     label=None, accepted_operations=None)
    arguments.update(changes)
    return asyncio.run(svc._forward_own_my_card_edit({"user_id": "alice"}, **arguments))


def test_a_persons_own_my_card_save_goes_to_the_project_transaction(monkeypatch):
    host = Host()
    svc = service(host)
    svc._card_coordinator = object()
    result = _my_route(monkeypatch, svc, accepted_operations={"svc": ["b"]})
    assert result["ok"] is True
    (_, _, body), = host.calls
    assert body["target"] == {"kind": "person_my", "principal_key": "user:alice", "original_revision": 5}
    assert body["selection"] == {"resource_operations": {"svc": ["a", "b"]}}
    assert body["accepted_operations"] == {"svc": ["b"]}
    assert _verified(body) is True


@pytest.mark.parametrize("case", ["transactions_off", "someone_elses", "no_forwarder"])
def test_the_my_card_route_is_left_to_the_ordinary_path_when_it_does_not_apply(monkeypatch, case):
    host = Host()
    svc = service(None if case == "no_forwarder" else host)
    if case != "transactions_off":
        svc._card_coordinator = object()
    result = _my_route(monkeypatch, svc, person="bob" if case == "someone_elses" else "alice")
    assert result is None  # the ordinary path keeps W578's direct-write refusal
    assert host.calls == []


@pytest.mark.parametrize("change", [dict(properties={"kept": False}), dict(composition_mode="or"),
                                    dict(label="Renamed"), dict(expected_card_revision=None)])
def test_a_my_card_save_the_project_cannot_bind_exactly_is_refused_and_nothing_is_sent(monkeypatch, change):
    host = Host()
    svc = service(host)
    svc._card_coordinator = object()
    result = _my_route(monkeypatch, svc, **change)
    assert result["ok"] is False
    assert host.calls == []


def test_unchanged_card_fields_sent_back_with_the_my_card_save_still_forward(monkeypatch):
    host = Host()
    svc = service(host)
    svc._card_coordinator = object()
    result = _my_route(monkeypatch, svc, properties={"kept": True}, composition_mode="and", label="My Card")
    assert result["ok"] is True and len(host.calls) == 1


def test_the_reviewed_catalog_travels_only_with_an_acceptance():
    """W723: the catalog the person reviewed rides with "Accept update", never alone."""
    host = Host()
    forward(service(host), accepted_operations={"svc": ["a"]}, expected_catalog_version="delegated_catalog_x")
    forward(service(host), expected_catalog_version="delegated_catalog_x")
    with_acceptance, without = (body for _, _, body in host.calls)
    assert with_acceptance["expected_catalog_version"] == "delegated_catalog_x"
    assert "expected_catalog_version" not in without and "accepted_operations" not in without

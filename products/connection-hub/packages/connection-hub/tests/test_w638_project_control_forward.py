"""W638: a managed project Control's (P) Save is forwarded to its project host, never written directly.

Both Hub save paths that refused a managed P (``control_card_update`` and the
owner's ``update_access``) now send it through the project's own transaction
with target kind ``project_control`` and the Card's storage coordinates. With
no forwarder they keep the existing direct-write refusal. A Save without a
route request id gets one stable per Save, so the same Save retried replays the
project's one decision. The host is a recording stand-in; PB's decision is
proven by PB's own tests.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import ACCESS_SOURCE_CONTROL, AutomationAccessService
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.managed_card_edit_forward import (
    OUTCOME_SCHEMA, TARGET_KINDS, ManagedCardEditError, PeerManagedCardEdit, managed_card_edit_body,
)

SECRET = "synthetic-managed-card-edit-key-32-bytes-or-more"
PROJECT = "work:project:synthetic-p"
CREATOR = "synthetic-creator"


class Host:
    def __init__(self, state="committed"):
        self.state, self.calls = state, []

    async def call(self, *, bundle_id, operation, data):
        # As the operation route sees it: the signed body under "data", the identity hints given as None
        # (else the platform adds the session's), and the answer inside the route's envelope.
        body = data["data"]
        assert data == {"data": body, "user_id": None, "fingerprint": None}
        self.calls.append((bundle_id, operation, body))
        return {"status": "ok", "bundle_id": bundle_id, operation: {"ok": True, "outcome": {
            "schema": OUTCOME_SCHEMA, "request_id": body["request_id"], "state": self.state,
            "transaction_id": "tx-p", "card_revision": 6}}}


def project_control(**changes):
    """A stored managed P: the application Control whose id is derived from its issuer ref and holder."""
    values = dict(access_id=control_card_id_for_issuer("application", PROJECT, grantor_subject=CREATOR),
                  issuer_kind="application", issuer_ref=PROJECT, grantor_subject=CREATOR,
                  source=ACCESS_SOURCE_CONTROL, delegate_subject="", expires_at=0,
                  properties={"kept": "as stored"}, composition_mode="", label="Project Control", control_card=None)
    values.update(changes)
    return SimpleNamespace(**values)


def service(host=None, *, managed=True):
    value = AutomationAccessService.__new__(AutomationAccessService)
    if managed:
        value._card_coordinator = object()
        value.bind_managed_control_scopes(["work:project:"])
    if host is not None:
        value.bind_managed_card_edit({"work:project:": PeerManagedCardEdit(
            call=host.call, bundle_id="problem-board@1-0", signer_id="synthetic-hub", secret=SECRET)})
    return value


def save(svc, record=None, *, request_id=None, user="alice", **changes):
    arguments = dict(resource_operations={"problem-board": ["project.cards.manage"]}, expected_card_revision=5,
                     properties={"kept": "as stored"}, composition_mode="and", label="Project Control")
    arguments.update(changes)
    return asyncio.run(svc._forward_managed_project_control({"user_id": user}, record=record or project_control(),
                                                            request_id=request_id, changes=arguments))


def test_project_control_is_a_forwarded_target_kind_with_storage_coordinates():
    assert "project_control" in TARGET_KINDS
    body = managed_card_edit_body(actor_subject="alice", project_ref=PROJECT, request_id="r-1", kind="project_control",
        principal_key="card:p-1", original_revision=2, selection={"resource_operations": {"svc": ["a"]}},
        access_id="p-1", subject_hash="h")
    assert body["target"] == {"kind": "project_control", "access_id": "p-1", "subject_hash": "h", "original_revision": 2}
    with pytest.raises(ManagedCardEditError):
        managed_card_edit_body(actor_subject="alice", project_ref=PROJECT, request_id="r-1", kind="project_control",
            principal_key="card:other", original_revision=2, selection={"resource_operations": {"svc": ["a"]}},
            access_id="p-1", subject_hash="h")


def test_a_managed_p_save_forwards_only_its_selection_with_ps_coordinates_and_project():
    host = Host()
    result = save(service(host), request_id="route-save-1")
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed"
    bundle, operation, data = host.calls[0]
    assert (bundle, operation) == ("problem-board@1-0", "project_card_edit")
    data = dict(data)
    data.pop("service_proof")
    record = project_control()
    assert data == {"schema": "managed-card-edit.v1", "actor_subject": "alice", "project_ref": PROJECT,
                    "request_id": "route-save-1",
                    "target": {"kind": "project_control", "access_id": record.access_id,
                               "subject_hash": subject_hash_for(CREATOR), "original_revision": 5},
                    "selection": {"resource_operations": {"problem-board": ["project.cards.manage"]}}}


def test_a_save_without_a_route_id_gets_one_stable_per_save():
    host = Host()
    svc = service(host)
    save(svc)
    save(svc)  # the same Save retried
    save(svc, resource_operations={"problem-board": []})  # a different selection
    save(svc, expected_card_revision=6)  # the same selection on a newer revision
    save(svc, user="bob")  # another person
    ids = [call[2]["request_id"] for call in host.calls]
    assert ids[0] == ids[1] and ids[0].startswith("project-control-save:")
    assert len({ids[0], ids[2], ids[3], ids[4]}) == 4


def test_without_a_forwarder_or_for_an_unmanaged_card_nothing_is_forwarded():
    # No forwarder: the caller keeps its direct-write refusal.
    assert save(service(None)) is None
    host = Host()
    # Card transactions disabled, another scope, another kind or a non-P id: not a managed P.
    assert save(service(host, managed=False)) is None
    assert save(service(host), project_control(issuer_ref="other:scope", access_id=control_card_id_for_issuer(
        "application", "other:scope", grantor_subject=CREATOR))) is None
    assert save(service(host), project_control(access_id="not-the-derived-id")) is None
    assert save(service(host), project_control(delegate_subject="someone")) is None
    assert host.calls == []


@pytest.mark.parametrize("change", [dict(label="Renamed"), dict(properties={"x": 1}), dict(composition_mode="or"),
                                    dict(expected_card_revision=None)])
def test_a_p_save_that_changes_more_than_its_selection_saves_nothing(change):
    host = Host()
    result = save(service(host), **change)
    assert result["ok"] is False and host.calls == []


@pytest.mark.parametrize("state", ["aborted", "pending"])
def test_a_refused_or_pending_p_save_keeps_the_draft(state):
    result = save(service(Host(state)))
    assert result["ok"] is False and result["error"] == "managed_card_edit_" + state and result["status"] == 409


@pytest.mark.parametrize("path", ["control_card_update", "update_access"])
def test_both_hub_save_paths_forward_a_managed_p_instead_of_refusing(path):
    host = Host()
    svc = service(host)
    record = project_control()

    async def load_record(access_id, *, grantor_subject):
        return record
    svc._load_record = load_record
    user = {"user_id": CREATOR}
    if path == "control_card_update":
        result = asyncio.run(svc.control_card_update(user, control_id=record.access_id,
            resource_operations={"problem-board": ["project.cards.manage"]}, expected_card_revision=5,
            request_id="route-save-2"))
    else:
        result = asyncio.run(svc.update_access(user, access_id=record.access_id,
            resource_grants={"problem-board": ["work:admin"]}, expected_card_revision=5, request_id="route-save-2"))
    assert result["ok"] is True, result
    assert host.calls and host.calls[0][2]["target"]["kind"] == "project_control"
    assert host.calls[0][2]["request_id"] == "route-save-2"


@pytest.mark.parametrize("path", ["control_card_update", "update_access"])
def test_both_paths_keep_the_direct_write_refusal_without_a_forwarder(path):
    svc = service(None)
    record = project_control()

    async def load_record(access_id, *, grantor_subject):
        return record
    svc._load_record = load_record
    user = {"user_id": CREATOR}
    if path == "control_card_update":
        result = asyncio.run(svc.control_card_update(user, control_id=record.access_id,
            resource_operations={"problem-board": []}, expected_card_revision=5))
    else:
        result = asyncio.run(svc.update_access(user, access_id=record.access_id,
            resource_grants={"problem-board": ["work:admin"]}, expected_card_revision=5))
    assert result["ok"] is False and result["error"] == "card_transactions_direct_write_refused"

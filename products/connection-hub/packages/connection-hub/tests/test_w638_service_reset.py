"""W638: the owner's per-service Reset is shown exactly, then committed only as shown.

Real My/Control Card values and the real composition and reset functions; the
Card store, the Control lookup and the project host are recording stand-ins.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.controls.project_person import (
    ProjectPersonControlIdentity, bind_project_person_control,
)
from connection_hub.delegated_credentials.managed_card_edit_forward import OUTCOME_SCHEMA, PeerManagedCardEdit
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card

PROJECT, PERSON, RESOURCE = "work:project:synthetic", "synthetic-person", "https://board.example.test/mcp"
OPERATIONS = ("project.cards.manage", "project.people.set_role")
SECRET = "synthetic-managed-card-edit-key-32-bytes-or-more"


def cards():
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=PERSON)
    control = CardAuthority(access_id=identity.control_id, client_id="control-card:synthetic",
        grantor_subject=identity.project_subject, delegate_subject="", source="control", card_kind="control",
        issuer_ref=PROJECT, issuer_kind="project", card_revision=3, catalog_version="synthetic-catalog",
        identity_scope="grantor", composition_mode="and", named_service_operations=NamedServiceSelection.none(),
        resource_grants={RESOURCE: ("work:admin",)}, resource_operations={RESOURCE: OPERATIONS},
        properties={"connection_hub.control_snapshot": {"schema": "connection_hub.control_snapshot.v1",
            "mode": "exact", "state": "exact", "basis_catalog_version": "synthetic-catalog"},
            "service_composition_modes": {RESOURCE: "and"}})
    control = bind_project_person_control(control, identity=identity)
    my = new_project_person_my_card(control_card=control, now=1_800_000_000)
    narrowed = replace(my, resource_operations={RESOURCE: ("project.people.set_role",)})
    return control, narrowed


class Host:
    def __init__(self):
        self.calls = []

    async def call(self, *, bundle_id, operation, data):
        self.calls.append(data)
        return {"ok": True, "outcome": {"schema": OUTCOME_SCHEMA, "request_id": data["request_id"],
                                        "state": "committed", "transaction_id": "tx-1", "card_revision": 2}}


def service(*, enabled=True, host=None):
    control, my = cards()
    value = AutomationAccessService.__new__(AutomationAccessService)

    async def load_record(access_id, *, grantor_subject):
        return record_from_card(my) if (access_id, grantor_subject) == (my.access_id, PERSON) else None

    async def compose(record):
        return record_from_card(control), None

    value._load_record, value._compose_with_control = load_record, compose
    value._managed_direct_write_refused = (lambda: {"ok": False, "error": "card_transactions_direct_write_refused"}) \
        if enabled else (lambda: None)
    direct = []

    async def reset_service_to_control(user, **kwargs):
        direct.append(kwargs)
        return {"ok": True, "direct": True}
    value.reset_service_to_control = reset_service_to_control
    if host is not None:
        value.bind_managed_card_edit({"work:project:": PeerManagedCardEdit(
            call=host.call, bundle_id="problem-board@1-0", signer_id="synthetic-hub", secret=SECRET)})
    return value, my, direct


def run(coroutine):
    return asyncio.run(coroutine)


def test_preview_shows_one_service_exactly_and_writes_nothing():
    svc, my, direct = service(host=Host())
    shown = run(svc.reset_service_preview({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE))
    assert shown["ok"] is True and len(shown["display_digest"]) == 64
    assert shown["preview"]["before"]["operations"] == ["project.people.set_role"]
    assert shown["preview"]["after"]["operations"] == sorted(OPERATIONS)
    assert direct == []


def test_confirm_forwards_exactly_the_shown_reset_when_transactions_are_enabled():
    host = Host()
    svc, my, direct = service(host=host)
    shown = run(svc.reset_service_preview({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE))
    result = run(svc.reset_service_confirm({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE,
        original_revision=my.card_revision, display_digest=shown["display_digest"], request_id="reset-1"))
    assert result["ok"] is True and result["managed_card_edit"]["state"] == "committed"
    (body,) = host.calls
    assert body["target"] == {"kind": "my_reset", "principal_key": "user:" + PERSON,
        "original_revision": my.card_revision, "resource": RESOURCE, "display_digest": shown["display_digest"]}
    assert body["selection"] == {} and body["actor_subject"] == PERSON and direct == []


def test_a_changed_display_another_person_or_no_forwarder_saves_nothing():
    host = Host()
    svc, my, direct = service(host=host)
    stale = run(svc.reset_service_confirm({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE,
        original_revision=my.card_revision, display_digest="0" * 64, request_id="reset-1"))
    assert stale["error"] == "card_reset_display_changed"
    other = run(svc.reset_service_preview({"user_id": "someone-else"}, access_id=my.access_id, resource=RESOURCE))
    assert other["ok"] is False
    unbound, my, _ = service(host=None)
    shown = run(unbound.reset_service_preview({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE))
    refused = run(unbound.reset_service_confirm({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE,
        original_revision=my.card_revision, display_digest=shown["display_digest"], request_id="reset-1"))
    assert refused["error"] == "card_transactions_direct_write_refused"
    assert host.calls == [] and direct == []


def test_with_transactions_off_the_owners_gated_reset_writer_is_used():
    svc, my, direct = service(enabled=False)
    shown = run(svc.reset_service_preview({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE))
    result = run(svc.reset_service_confirm({"user_id": PERSON}, access_id=my.access_id, resource=RESOURCE,
        original_revision=my.card_revision, display_digest=shown["display_digest"], request_id="reset-1"))
    assert result == {"ok": True, "direct": True}
    assert direct == [{"access_id": my.access_id, "resource": RESOURCE,
                       "expected_card_revision": my.card_revision, "request_id": "reset-1"}]

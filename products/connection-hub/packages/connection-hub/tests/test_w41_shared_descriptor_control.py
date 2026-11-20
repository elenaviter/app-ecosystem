"""W41: one descriptor Control per agent, written only by its two writers.

The operator's W212 ruling: the Control "is not per user... its for all users".
The deployment publishes it; a platform administrator edits it on the
application's behalf; every user's Agent Card composes with the same one. A
user's sync, a user's Save and an agent bearer never write it (T1). A
repeated or concurrent publication writes once (T3b), and a Card still bound
to its owner's legacy per-user copy moves to the shared Control (T11).
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.automation_access import card_authority_from_record
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE
from connection_hub.delegated_credentials.controls.descriptor_control import application_authority_subject
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch, effective_card_authority
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from test_agent_capability_control_sync import (
    APPLICATION, HOLDER, NAMED_RESOURCE, OWNER, PROJECT, RESOURCE, TENANT, _policy_with_named_operations, _service,
)

READ = "object.list"
POST = "object.action.post_message"
ADMIN = {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}
SECOND = "user-2"


def _arguments(*operations: str, revision: str = "descriptor-r1") -> dict:
    return {
        "application": APPLICATION, "agent_id": "worker", "descriptor_revision": revision,
        "descriptor_payload": {"revision": revision},
        "capability_authority": _policy_with_named_operations(*operations),
        "selected_capabilities": _policy_with_named_operations(*operations),
        "resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:read",
                                             *(["slack:post"] if POST in operations else [])]},
        "resource_operations": {NAMED_RESOURCE: ["named_services_call"]},
        "named_service_operations": {NAMED_RESOURCE: {"slack": list(operations)}},
        "selected_resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:read",
                                                      *(["slack:post"] if POST in operations else [])]},
        "selected_resource_operations": {NAMED_RESOURCE: ["named_services_call"]},
        "selected_named_service_operations": {NAMED_RESOURCE: {"slack": list(operations)}},
    }


async def _publish(service, *operations: str, revision: str = "descriptor-r1") -> dict:
    return await service.publish_agent_descriptor_control({"bundle_id": APPLICATION},
                                                          **_arguments(*operations, revision=revision))


def _named(card) -> set[str]:
    return set(card.named_service_operations.operations.get(NAMED_RESOURCE, {}).get("slack", ()))


def test_the_holder_is_the_applications_synthetic_subject() -> None:
    assert HOLDER.startswith("application-authority:")
    assert HOLDER == application_authority_subject(tenant=TENANT, project=PROJECT, application=APPLICATION)
    assert HOLDER != application_authority_subject(tenant=TENANT, project=PROJECT, application="other@1-0")


@pytest.mark.asyncio
async def test_every_user_composes_with_the_one_published_control() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    assert published["ok"] and published["control_changed"], published
    control_id = published["control_card"]["access_id"]
    control = persistence.records[control_id][0]
    assert control.grantor_subject == HOLDER

    for subject in (OWNER, SECOND):
        synced = await service.user_sync_only({"user_id": subject}, **_arguments(READ, POST))
        assert synced["ok"], synced
        assert synced["control_card"]["access_id"] == control_id
        card = persistence.records[synced["card"]["access_id"]][0]
        assert card.grantor_subject == subject
        assert (card.control_card.holder_subject, card.control_card.control_id) == (HOLDER, control_id)
    # One Control for both users, no per-user copy.
    controls = [record for record, _ in persistence.records.values() if record.source == "control"]
    assert [record.access_id for record in controls] == [control_id]


@pytest.mark.asyncio
async def test_an_administrator_edit_reaches_every_user_at_once() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    second = await service.user_sync_only({"user_id": SECOND}, **_arguments(READ, POST))
    assert second["ok"], second
    card = persistence.records[second["card"]["access_id"]][0]
    control = persistence.records[published["control_card"]["access_id"]][0]
    assert POST in _named(effective_card_authority(card_authority_from_record(card), card_authority_from_record(control)))

    narrowed = await service.update_access(
        ADMIN, access_id=control.access_id, holder_subject=HOLDER,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]}, resource_operations={},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ]}}, account_scope={},
        expected_card_revision=control.card_revision, expected_catalog_version=control.catalog_version,
        composition_mode="and", properties=control.properties)
    assert narrowed["ok"], narrowed

    # The second user's Card was not written, and its next call is narrowed.
    assert persistence.records[card.access_id][0] == card
    stored = persistence.records[control.access_id][0]
    assert stored.grantor_subject == HOLDER and stored.card_revision == control.card_revision + 1
    effective = effective_card_authority(card_authority_from_record(card), card_authority_from_record(stored))
    assert POST not in _named(effective) and READ in _named(effective)


@pytest.mark.asyncio
async def test_t1_an_ordinary_user_never_writes_the_shared_control() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    control = persistence.records[published["control_card"]["access_id"]][0]
    user = {"user_id": SECOND, "roles": ["kdcube:role:registered"]}
    change = dict(resource_grants={}, expected_card_revision=control.card_revision)

    # Not in the user's own partition, and not through the holder either.
    assert (await service.update_access(user, access_id=control.access_id, **change))["error"] == (
        "delegated_access_not_found")
    assert (await service.update_access(user, access_id=control.access_id, holder_subject=HOLDER, **change)) == {
        "ok": False, "error": "platform_admin_required", "status": 403}
    assert (await service.control_card_update(user, control_id=control.access_id, holder_subject=HOLDER,
                                              resource_grants={})) == {
        "ok": False, "error": "platform_admin_required", "status": 403}
    assert (await service.control_card_get(user, control_id=control.access_id, holder_subject=HOLDER))["status"] == 403
    assert (await service.return_control_to_descriptor(user, access_id=control.access_id, holder_subject=HOLDER,
                                                       expected_card_revision=control.card_revision))["status"] == 403
    assert persistence.records[control.access_id][0] == control


@pytest.mark.asyncio
async def test_t1_a_users_sync_reads_the_control_and_never_publishes() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    control = persistence.records[published["control_card"]["access_id"]][0]

    # A newer descriptor in the user's runtime does not reach the Control by the user's sync.
    stale = await service.user_sync_only({"user_id": OWNER}, **_arguments(READ, revision="descriptor-r2"))
    assert stale == {"ok": False, "error": "agent_descriptor_control_not_current", "retryable": True,
                     "status": 409, "control_revision": control.card_revision}
    assert persistence.records[control.access_id][0] == control

    unpublished, _ = _service(named_services=True)
    missing = await unpublished.user_sync_only({"user_id": OWNER}, **_arguments(READ))
    assert missing["error"] == "agent_descriptor_control_not_published" and missing["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", [
    {"user_id": OWNER, "bundle_id": APPLICATION},              # a user-bound call
    {"user_id": f"integration:agent:{OWNER}", "bundle_id": APPLICATION},  # an agent bearer
    {"bundle_id": "other-app@1-0"},                            # another application's deployment
    {},
])
async def test_t1_only_the_applications_own_deployment_publishes(deployment) -> None:
    service, persistence = _service(named_services=True)
    refused = await service.publish_agent_descriptor_control(deployment, **_arguments(READ, POST))
    assert refused == {"ok": False, "error": "agent_descriptor_publication_requires_deployment", "status": 403}
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_t1_a_forged_holder_names_no_control() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    control_id = published["control_card"]["access_id"]
    other_holder = application_authority_subject(tenant=TENANT, project=PROJECT, application="other@1-0")
    for holder in (other_holder, OWNER, "application-authority:not-a-digest"):
        answer = await service.control_card_get(ADMIN, control_id=control_id, holder_subject=holder)
        assert answer["ok"] is False and answer["status"] == 404, (holder, answer)


@pytest.mark.asyncio
async def test_t3b_a_repeated_publication_writes_once() -> None:
    service, persistence = _service(named_services=True)
    first = await _publish(service, READ, POST)
    writes = len(persistence.persisted)
    again = await _publish(service, READ, POST)
    assert first["control_changed"] is True and again["control_changed"] is False
    assert len(persistence.persisted) == writes


@pytest.mark.asyncio
async def test_t3b_a_concurrent_publication_of_the_same_revision_is_a_no_op() -> None:
    service, persistence = _service(named_services=True)
    first = await _publish(service, READ, POST)
    control = persistence.records[first["control_card"]["access_id"]][0]
    load = service._load_record_any_state
    raced = []

    async def stale_read(access_id, *, grantor_subject):
        # The losing instance read the Control before the winner's write landed.
        if access_id == control.access_id and not raced:
            raced.append(True)
            stored = await load(access_id, grantor_subject=grantor_subject)
            return (dataclasses.replace(stored[0], card_revision=stored[0].card_revision - 1,
                                        resource_acceptance={}), stored[1])
        return await load(access_id, grantor_subject=grantor_subject)

    service._load_record_any_state = stale_read
    loser = await _publish(service, READ, POST)
    assert loser["ok"] and loser["control_changed"] is False, loser
    assert loser["control_card"]["card_revision"] == control.card_revision
    assert persistence.records[control.access_id][0] == control


@pytest.mark.asyncio
async def test_t11_a_card_on_its_owners_legacy_copy_moves_to_the_shared_control() -> None:
    service, persistence = _service(named_services=True)
    published = await _publish(service, READ, POST)
    synced = await service.user_sync_only({"user_id": OWNER}, **_arguments(READ, POST))
    card, handles = persistence.records[synced["card"]["access_id"]]
    shared = persistence.records[published["control_card"]["access_id"]][0]
    legacy_id = control_card_id_for_issuer("kdcube_agent_descriptor", RESOURCE, grantor_subject=OWNER)
    legacy = dataclasses.replace(shared, access_id=legacy_id, grantor_subject=OWNER)
    persistence.records[legacy_id] = (legacy, persistence.records[shared.access_id][1])
    persistence.records[card.access_id] = (dataclasses.replace(card, control_card=dataclasses.replace(
        card.control_card, control_id=legacy_id, holder_subject="")), handles)

    moved = await service.user_sync_only({"user_id": OWNER}, **_arguments(READ, POST))
    assert moved["ok"], moved
    rebound = persistence.records[card.access_id][0].control_card
    assert (rebound.holder_subject, rebound.control_id) == (HOLDER, shared.access_id)


def test_t11_a_card_bound_to_a_revoked_control_is_refused() -> None:
    import asyncio

    async def scenario():
        service, persistence = _service(named_services=True)
        published = await _publish(service, READ, POST)
        synced = await service.user_sync_only({"user_id": OWNER}, **_arguments(READ, POST))
        card = persistence.records[synced["card"]["access_id"]][0]
        control = persistence.records[published["control_card"]["access_id"]][0]
        return card, dataclasses.replace(control, state="revoked")

    card, revoked = asyncio.run(scenario())
    assert revoked.state != CARD_STATE_ACTIVE
    with pytest.raises(ControlCardMismatch, match="control_card_not_active"):
        effective_card_authority(card, revoked)

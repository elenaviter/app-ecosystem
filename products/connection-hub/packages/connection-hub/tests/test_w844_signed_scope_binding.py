"""Signed scopes bind the real planner's loaded targets, including no-op reads.

Real C/My/P authority and planner; catalog resolution and persistence are test
adapters. These checks are not a caller-to-project entitlement registry.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import CardLifecyclePlanOperation
from connection_hub.delegated_credentials.cards.participant_card_version import REFUSALS
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from test_w578_lifecycle_plan_operation import _Port, _request as plan_request, _verified as verified_plan
from test_w661_card_version_operation import _operation, _request, _verified
from test_w661_role_selection_plan import _Host, _role, _world
from test_w607_existing_card_selection_plan import ACTOR, PROJECT, TARGET
from test_w850_agent_role_follow_plan import _agent

OTHER_PROJECT = "work:project:another-project"


class _CountedHost(_Host):
    def __init__(self, *cards):
        super().__init__(*cards)
        self.card_reads = []
        self.catalog_reads = 0
        self.config_reads = 0

    def _cards(self):
        original = super()._cards()
        reads = self.card_reads

        class Cards:
            async def load_current(self, access_id, *, subject_hash):
                reads.append((subject_hash, access_id))
                return await original.load_current(access_id, subject_hash=subject_hash)

        return Cards()

    async def _active_catalog(self):
        self.catalog_reads += 1
        return await super()._active_catalog()

    async def _catalog_config(self, active, *, owner_subject):
        self.config_reads += 1
        return await super()._catalog_config(active, owner_subject=owner_subject)


class _NoStageStore:
    """Neither a refusal nor an already-applied answer may reach persistence."""

    async def stage(self, *args, **kwargs):
        pytest.fail("a scope refusal or a role no-op reached STAGE persistence")


def _fixture(card_kind):
    project, control, my = _world()
    if card_kind == "project":
        project = dataclasses.replace(project, resource_grants=control.resource_grants,
                                      resource_operations=control.resource_operations)
    card = {"control": control, "my": my, "project": project}[card_kind]
    host = _CountedHost(project, control, my)
    return host, card


def _assert_only_existing_reads(host, card, before):
    # No callback to a project host, no catalog config/resolver for a no-op,
    # exactly the catalog and the target that the original path already reads.
    assert host.card_reads == [(_role(card)["subject_hash"], card.access_id)]
    assert host.catalog_reads == 1
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0
    assert {key: value.to_dict() for key, value in host.by_id.items()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_card_version_own_project_noop_stays_already_applied_with_existing_io_only(card_kind):
    host, card = _fixture(card_kind)
    before = copy.deepcopy({key: value.to_dict() for key, value in host.by_id.items()})
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    request = _request(scope=PROJECT, actor_subject=ACTOR,
                       updates=[_role(card, add_grants=["work:read"])])
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "already_applied"}
    assert len(operation._nonces.keys) == 1
    _assert_only_existing_reads(host, card, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind,scope,target", [
    ("control", OTHER_PROJECT, TARGET),
    ("my", OTHER_PROJECT, TARGET),
    ("control", PROJECT, "someone-else"),
    ("my", PROJECT, "someone-else"),
    ("project", PROJECT, TARGET),
])
async def test_card_version_role_noop_cannot_disclose_a_foreign_target(card_kind, scope, target):
    host, card = _fixture(card_kind)
    before = copy.deepcopy({key: value.to_dict() for key, value in host.by_id.items()})
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    # Both project scopes pass the same configured prefix. Only the loaded
    # target's identity can tell whether this request names its own project.
    request = _request(scope=scope, actor_subject=ACTOR,
                       updates=[_role(card, target_subject=target, add_grants=["work:read"])])
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "edit_invalid", "status": REFUSALS["edit_invalid"]}, result
    assert len(operation._nonces.keys) == 1  # authenticated refusal consumes just its documented nonce
    _assert_only_existing_reads(host, card, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_signed_lifecycle_plan_also_binds_role_noop_before_answering(card_kind):
    host, card = _fixture(card_kind)
    stage, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    port = _Port()
    operation = CardLifecyclePlanOperation(
        callers=stage._callers, authorization=port, planner=plan_card_lifecycle,
        host=host, nonces=stage._nonces, clock=stage._clock)
    request = plan_request(creations=[], scope=OTHER_PROJECT, actor=ACTOR,
                           updates=[_role(card, add_grants=["work:read"])])
    result = verified_plan(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "card_plan_update_scope_invalid", "status": 409}, result
    assert len(port.requests) == 1  # its existing PLAN authorization; no second lookup
    assert len(operation._nonces.keys) == 1
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_foreign_role_policy_cannot_disclose_an_unknown_operation(card_kind):
    host, card = _fixture(card_kind)
    stage, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    operation = CardLifecyclePlanOperation(
        callers=stage._callers, authorization=_Port(), planner=plan_card_lifecycle,
        host=host, nonces=stage._nonces, clock=stage._clock)
    # On the base this reveals that the Card holds an operation absent from
    # the supplied map. The target must be bound before any policy delta.
    request = plan_request(creations=[], scope=OTHER_PROJECT, actor=ACTOR,
                           updates=[_role(card, remove_operations=["absent.operation"], operation_grants={})])
    assert verified_plan(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_plan_update_scope_invalid", "status": 409}
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0


def _target_update(kind, card, *, target=TARGET, **extra):
    return {"kind": kind, "target_subject": target, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject),
            "original_revision": card.card_revision, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "control-reselect", "my-reselect", "project-reselect", "agent-reselect", "invitation-reselect",
    "my-reset", "my-settings", "control-remove", "my-remove", "invitation-revoke",
    "control-attach", "agent-detach", "agent-profile", "agent-role-noop", "project-retire",
])
async def test_every_existing_target_stage_class_refuses_another_projects_scope(path):
    from connection_hub.delegated_credentials.controls.model import new_credentialless_card
    from connection_hub.delegated_credentials.controls.project_invitation import (
        PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlIdentity, bind_project_invitation_control,
    )
    from connection_hub.delegated_credentials.project_identity_lifecycle import MY_CARD_COMMIT_EMAIL_PROPERTY

    project, control, my = _world()
    agent = _agent(project)
    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invite-scope-test", target_email="invitee@example.test")
    invitation = bind_project_invitation_control(new_credentialless_card(
        control_id=identity.control_id, grantor_subject=identity.project_subject, catalog_version="catalog-v1",
        issuer_ref=identity.invitation_ref, issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=100),
        identity=identity)
    host = _CountedHost(project, control, my, agent, invitation)
    updates = {
        "control-reselect": _target_update("reselect", control, selection={}),
        "my-reselect": _target_update("reselect", my, selection={}),
        "project-reselect": _target_update("reselect_project_control", project,
                                            target=project.access_id, selection={}),
        "agent-reselect": _target_update("reselect_agent_card", agent, target=agent.access_id, selection={}),
        "invitation-reselect": _target_update("reselect_invitation_control", invitation,
                                               target=invitation.access_id, selection={}),
        "my-reset": _target_update("reset_to_control", my, resource="problem-board", display_digest="d" * 64,
                                     control={"access_id": control.access_id,
                                              "subject_hash": subject_hash_for(control.grantor_subject)}),
        "my-settings": _target_update("set_person_properties", my,
                                        selection={"properties": {MY_CARD_COMMIT_EMAIL_PROPERTY: None}}),
        "control-remove": _target_update("remove_person", control),
        "my-remove": _target_update("remove_person", my),
        "invitation-revoke": _target_update("revoke", invitation, target=identity.invitation_ref),
        "control-attach": _target_update("attach", dataclasses.replace(control, control_card=None),
                                          parent={"access_id": project.access_id, "holder_subject": ACTOR}),
        "agent-detach": _target_update("detach_agent", agent, target=agent.delegate_subject),
        "agent-profile": _target_update("apply_agent_profile", agent, target=agent.delegate_subject,
                                         profile="worker", resource="problem-board"),
        "agent-role-noop": _role(agent, target_subject=agent.access_id, add_grants=["work:read"]),
        "project-retire": _target_update("retire_project_control", project, target=OTHER_PROJECT),
    }
    if path == "control-attach":
        host.by_id[control.access_id] = dataclasses.replace(control, control_card=None)
    actor = TARGET if path == "my-settings" else ACTOR
    before = copy.deepcopy({key: value.to_dict() for key, value in host.by_id.items()})
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    request = _request(scope=OTHER_PROJECT, actor_subject=actor, updates=[updates[path]])
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "edit_invalid", "status": REFUSALS["edit_invalid"]}
    assert len(operation._nonces.keys) == 1
    assert len(host.card_reads) == 1 and host.catalog_reads == 1
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0
    assert {key: value.to_dict() for key, value in host.by_id.items()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application_control", "project_person_control", "project_person_my_card"])
async def test_stage_creation_cannot_use_a_foreign_issuer_or_parent(kind):
    project, control, my = _world()
    host = _CountedHost(project, control, my)
    creations = {
        "application_control": {"ref": "p", "kind": kind,
                                "identity": {"holder_subject": ACTOR, "issuer_ref": PROJECT},
                                "selection": {}, "parent": None},
        "project_person_control": {"ref": "c", "kind": kind, "identity": {"target_subject": TARGET},
                                   "selection": {},
                                   "parent": {"access_id": project.access_id, "holder_subject": ACTOR}},
        "project_person_my_card": {"ref": "my", "kind": kind, "identity": {"person_subject": TARGET},
                                   "selection": {},
                                   "parent": {"access_id": control.access_id,
                                              "holder_subject": control.grantor_subject}},
    }
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    request = _request(scope=OTHER_PROJECT, actor_subject=ACTOR, updates=[], creations=[creations[kind]])
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "edit_invalid", "status": REFUSALS["edit_invalid"]}
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [PROJECT, OTHER_PROJECT])
async def test_new_invitation_identity_is_derived_from_the_signed_scope(scope):
    from connection_hub.delegated_credentials.controls.project_invitation import ProjectInvitationControlIdentity

    host = _CountedHost()
    stage, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    operation = CardLifecyclePlanOperation(
        callers=stage._callers, authorization=_Port(), planner=plan_card_lifecycle,
        host=host, nonces=stage._nonces, clock=stage._clock)
    request = plan_request(scope=scope, actor=ACTOR, creations=[{
        "ref": "invitation", "kind": "project_invitation_control",
        "identity": {"invitation_ref": "new-invitation", "target_email": "invitee@example.test"},
        "selection": {}, "parent": None,
    }])
    result = verified_plan(await operation.answer(request), request)
    assert result["kind"] == "plan", result
    [member] = result["plan"]["candidate_value"]["cards"]
    expected = ProjectInvitationControlIdentity.build(
        project_ref=scope, invitation_ref="new-invitation", target_email="invitee@example.test")
    assert member["access_id"] == expected.control_id
    assert member["subject_hash"] == subject_hash_for(expected.project_subject)
    assert host.writes == 0  # one configured participant may serve both projects; no new entitlement registry


async def _role_commit_world(tmp_path, card_kind):
    """Real planner and version store; catalog resolution and lock are adapters."""
    from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore
    from connection_hub.delegated_credentials.cards.participant_card_version import CardVersionRefused
    from test_card_transaction_store import _setup
    from test_w661_card_version_integration import _Catalogs

    store, service, _, _ = await _setup(tmp_path)
    host, card = _fixture(card_kind)
    for authority in host.by_id.values():
        await service.commit(authority, subject_hash=subject_hash_for(authority.grantor_subject),
                             expected_revision=0, now=100)
    catalog = {"version": "catalog-v2", "content_hash": "c" * 64}
    port = ServiceCardVersionStore(service, refused=CardVersionRefused, catalog_store=_Catalogs(catalog))
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=port)
    request = _request(scope=PROJECT, actor_subject=ACTOR, catalog=catalog,
                       updates=[_role(card, add_grants=["work:write"])])
    return store, operation, host, card, request


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_own_project_role_change_reaches_real_stage_and_publish(tmp_path, card_kind):
    store, operation, host, card, request = await _role_commit_world(tmp_path, card_kind)
    staged = _verified(await operation.answer(request), request)
    assert staged["kind"] == "staged", staged
    assert staged["members"][0]["base_version"] == card.card_revision
    publish = _request("publish", scope=PROJECT)
    assert _verified(await operation.answer(publish), publish)["kind"] == "published"
    current = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                access_id=card.access_id)
    assert current[1].card_revision == card.card_revision + 1
    assert set(current[1].resource_grants["problem-board"]) == {"work:read", "work:write"}


def _snapshot(store):
    return {str(path.relative_to(store.root)): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("op,published", [
    ("publish", False), ("rollback", False), ("outcome", False), ("compensate", False),
    ("rollback", True), ("outcome", True), ("compensate", True),
])
async def test_signed_recovery_never_returns_or_changes_foreign_versions(tmp_path, op, published):
    from test_w661_card_version_integration import _world as store_world, _call, _update
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from test_w661_card_version_operation import TXN

    store, operation, before, _ = await store_world(tmp_path)
    staged = await _call(operation, "stage", updates=_update(before))
    if published:
        await _call(operation, "publish")
    assert tx.card_version_marker_path(store, TXN).exists() is not published
    snapshot = _snapshot(store)
    nonce_count = len(operation._nonces.keys)
    kwargs = {} if op == "publish" else {"links": staged["members"]}
    if op == "compensate" and not published:
        # No published version of this txn is current, so neither scope can
        # restore it. The existing superseded refusal returns no Card/link
        # data. Do not rename it just to manufacture another scope-code diff.
        expected = {"kind": "refused", "code": "compensation_superseded", "status": 409}
        assert await _call(operation, op, **kwargs) == expected
        nonce_count += 1
    else:
        expected = {"kind": "refused", "code": "request_scope_invalid", "status": 403}
    assert await _call(operation, op, scope=OTHER_PROJECT, **kwargs) == expected
    assert _snapshot(store) == snapshot and len(operation._nonces.keys) == nonce_count + 1


@pytest.mark.asyncio
async def test_signed_census_uses_only_ids_derived_from_its_scope(tmp_path):
    from test_card_census_read import ADMIN, _world as census_world, _request as census_request, _verified as verify

    operation, store, control, identity, _ = await census_world(tmp_path)
    before = _snapshot(store)
    request = census_request([ADMIN], scope=OTHER_PROJECT, include_catalog=False)
    entry = verify(await operation.answer(request), request)["persons"][0]
    assert entry["control"]["state"] == entry["my"]["state"] == "absent"
    assert entry["control"]["access_id"] != identity.control_id
    assert entry["my"]["access_id"] != identity.my_card_id
    assert _snapshot(store) == before and len(operation._nonces.keys) == 1


@pytest.mark.asyncio
async def test_signed_invitation_census_lists_only_its_scope_partition(tmp_path):
    from test_w814_invitation_census_read import (
        OTHER_PROJECT as invitation_scope, _world as invitation_world,
        _request as invitation_request, _verified as verify,
    )

    operation, store, cards = await invitation_world(tmp_path)
    before = _snapshot(store)
    request = invitation_request(scope=invitation_scope)
    result = verify(await operation.answer(request), request)
    assert result["count"] == 1
    assert {row["access_id"] for row in result["invitations"]} == {cards["other"].access_id}
    assert _snapshot(store) == before and len(operation._nonces.keys) == 1


@pytest.mark.asyncio
async def test_signed_collection_contains_only_its_scopes_derived_targets(tmp_path):
    from connection_hub.delegated_credentials.cards.card_read_collection import resolve_collection
    from test_w502_card_read_collection_register import (
        ADMIN, _world as collection_world, _request as collection_request, _verified as verify,
    )

    operation, _, store, control, identity, _ = await collection_world(tmp_path)
    request = collection_request([ADMIN], scope=OTHER_PROJECT)
    result = verify(await operation.answer(request), request)
    assert result["kind"] == "collection" and result["ref"]["scope"] == OTHER_PROJECT
    header, reads = await resolve_collection(store, result["ref"]["collection_id"])
    assert header["scope"] == OTHER_PROJECT
    assert len(reads) == 2 and {row["revision"] for row in reads} == {0}
    assert not {identity.control_id, identity.my_card_id} & {row["access_id"] for row in reads}
    assert len(operation._nonces.keys) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["prepare", "read_pending", "finish"])
async def test_legacy_signed_participant_requires_the_verified_intents_scope(tmp_path, action):
    from test_card_participant_operation import TX, _world as transaction_world, _request as request_for, _verified
    from connection_hub.delegated_credentials.cards import transaction_store as tx

    world, _, _ = await transaction_world(tmp_path)
    # The authority adapter returns the same signed intent even for the wrong
    # lookup scope: the participant still must compare the verified payload.
    world.records[(OTHER_PROJECT, TX)] = world.record
    before = _snapshot(world.store)
    request = request_for(action, scope=OTHER_PROJECT, decision="committed" if action == "finish" else None)
    assert _verified(await world.operation().answer(request), request)["code"] == "card_intent_not_bound"
    assert await tx.state(world.store, transaction_id=TX) is None
    assert _snapshot(world.store) == before and len(world.nonces.keys) == 1

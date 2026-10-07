# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W595 B3: planned Card groups committed through the real Card store and read back by the census.

B3: "actual P/C/My genesis, join and redemption, honest revisions/absence,
born-bound Control and complete authoritative provider/census/anchor
behavior." This is the Hub side, end to end at package level: the W594
planner reading through the production ``DurableCardPersistence``, the real
file Card store and ``DelegatedCardService`` group staging and decision, and
the signed ``card_census_read`` operation over the same store.

Labelled stand-ins: the transaction decision source (``Decisions``) and the
catalog reservations, as in ``test_w578_card_lifecycle_plan.py``; the census
caller's shared secrets. Problem Board's participant, its anchor and the
mounted transport are not here (B2, B6).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import (
    build_project_person_control,
    plan_card_lifecycle,
)
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.census_read import CardCensusReadOperation
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED, CardAuthority
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card
from test_card_census_read import PEER, RECEIPT_SECRET, REQUEST_SECRET, NOW, _Nonces, _request, _verified
from test_card_service import _Cache
from test_card_transaction_store import Decisions
from test_w578_card_lifecycle_plan import _authorization

PROJECT = "work:project:b3"
CREATOR = "user:b3-creator"
PEOPLE = {name: f"user:b3-{name}" for name in ("alice", "bob", "carol", "dave")}


class _Reservations:
    """Stand-in for the catalog reservation port: records what staging reserves."""

    def __init__(self):
        self.held = []

    async def reserve(self, **kwargs):
        self.held.append(kwargs)

    async def release(self, transaction_id, *, intent_digest):
        self.held = [item for item in self.held if item["transaction_id"] != transaction_id]


class _World:
    def __init__(self, tmp_path):
        @asynccontextmanager
        async def mutation_lock(**kwargs):
            yield

        self.store = BundleStorageDelegatedCardStore(tmp_path / "cards")
        self.decisions = Decisions()
        tx.bind_transaction_decisions(self.store, self.decisions)
        self.reservations = _Reservations()
        tx.bind_catalog_reservations(self.store, self.reservations)
        self.service = DelegatedCardService(store=self.store, cache=_Cache(), mutation_lock=mutation_lock)
        # The production persistence the planner reads through (host._cards()).
        self.persistence = DurableCardPersistence(
            redis=object(), tenant="b3", project="b3", card_store=self.store,
            credential_handles=MagicMock(), mutation_lock=mutation_lock)
        self.catalog = CatalogDocument.build({"delegated_credentials": {"oauth": {"resources": []}}})
        caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                                   receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                                   hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                                   census_scope_prefix="work:project:")
        self.census_operation = CardCensusReadOperation(
            callers={PEER: caller}, card_store=self.store, catalog_store=None,
            nonces=_Nonces(), clock=lambda: NOW)
        self.groups = 0
        self.host = self  # the planner's host; a real AutomationAccessService may replace it

    # host surface the planner uses
    def _cards(self):
        return self.persistence

    async def _active_catalog(self):
        return self.catalog

    @staticmethod
    def _version_of(active):
        return active.version

    async def plan(self, creations, updates=(), *, grants=None):
        return await plan_card_lifecycle(
            self.host, project_ref=PROJECT, creations=creations, updates=updates,
            actor_subject=CREATOR, actor_kind="caller", request_id="b3-request",
            authorization=_b3_authorization(creations, updates, grants=grants))

    async def card(self, authority: CardAuthority) -> CardAuthority | None:
        loaded = await self.store.read_current_authority(
            subject_hash=subject_hash_for(authority.grantor_subject), access_id=authority.access_id)
        return None if loaded is None else loaded[1]

    async def commit_plan(self, plan) -> dict:
        """Stage the plan's exact members as ONE group, record the decision, decide it."""
        members = []
        for member in plan["candidate_value"]["cards"]:
            candidate = CardAuthority.from_mapping(member["candidate"])
            original = None
            if not member["original_absent"]:
                loaded = await self.store.read_current_authority(
                    subject_hash=member["subject_hash"], access_id=member["access_id"])
                original = loaded[1]
            members.append((member["subject_hash"], original, candidate, member["action"]))
        self.groups += 1
        group_id, intent = f"{self.groups:064x}", f"{self.groups + 100:064x}"
        staged = await self.service.stage_group_transaction(
            transaction_id=group_id, intent_digest=intent, participant="project", members=members,
            now=datetime.fromtimestamp(NOW, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
        if staged.get("staged") is not True:
            return staged
        self.decisions.recorded[group_id] = "committed"
        return await self.service.decide_group_transaction(
            transaction_id=group_id, intent_digest=intent, decision="committed", now=NOW)

    async def census(self, *persons):
        request = _request(persons, scope=PROJECT, include_catalog=False)
        result = _verified(await self.census_operation.answer(request), request)
        assert result["kind"] == "census", result
        return {entry["person"]: entry for entry in result["persons"]}

    def files(self):
        return {path: path.read_bytes() for path in self.store.root.rglob("*") if path.is_file()}


def _b3_authorization(creations, updates, *, grants=None):
    authorization = _authorization(creations, updates)
    # The shared helper names its own actor and project; bind this world's.
    request = replace(authorization.request, actor_subject=CREATOR, project_ref=PROJECT, request_id="b3-request")
    from connection_hub.delegated_credentials.project_authorization import (
        LifecyclePlanAuthorization, ProjectAuthorizationDecision,
    )
    decisions = tuple((step.ref, ProjectAuthorizationDecision.allow(
        request.step_request(step),
        delegable_grants=(grants or {}).get(step.ref, ("work:admin",)),
        platform_admin=grants is None,
        project_control=dict(authorization.decisions)[step.ref].project_control,
    )) for step in request.steps)
    return LifecyclePlanAuthorization(request=request, decisions=decisions)


def _p_request():
    return {"ref": "p", "kind": "application_control",
            "identity": {"holder_subject": CREATOR, "issuer_ref": PROJECT}, "selection": {}, "parent": None}


def _c_request(person, parent, ref="c"):
    return {"ref": ref, "kind": "project_person_control",
            "identity": {"target_subject": person}, "selection": {}, "parent": parent}


def _my_request(person, parent_ref="c"):
    return {"ref": "my", "kind": "project_person_my_card",
            "identity": {"person_subject": person}, "selection": {}, "parent": {"ref": parent_ref}}


def _live_p(p: CardAuthority) -> dict:
    return {"access_id": p.access_id, "holder_subject": CREATOR}


def _by_kind(plan):
    cards = [CardAuthority.from_mapping(member["candidate"]) for member in plan["candidate_value"]["cards"]]
    p = next(card for card in cards if card.issuer_kind == "application")
    rest = [card for card in cards if card is not p]
    return p, rest


async def _genesis(world, person=PEOPLE["alice"]):
    plan = await world.plan([_my_request(person), _c_request(person, {"ref": "p"}), _p_request()])
    assert plan["ok"] is True, plan
    decided = await world.commit_plan(plan["plan"])
    assert decided["state"] == "committed", decided
    p = next(CardAuthority.from_mapping(member["candidate"]) for member in plan["plan"]["candidate_value"]["cards"]
             if member["candidate"]["issuer_kind"] == "application")
    return plan["plan"], await world.card(p)


def _assert_complete(entry, *, control: CardAuthority, ancestors: list[CardAuthority]):
    assert entry["edge"] == {"state": "valid"}, entry
    assert entry["chain"]["state"] == "complete", entry
    chain = [(card["access_id"], card["revision"]) for card in entry["chain"]["cards"]]
    assert chain == [(card.access_id, card.card_revision) for card in [control, *ancestors]], entry


@pytest.mark.asyncio
async def test_b3_genesis_commits_p_c_my_as_one_group_and_the_census_reads_a_complete_chain(tmp_path):
    world = _World(tmp_path)
    alice = PEOPLE["alice"]
    plan, p = await _genesis(world)
    assert plan["reads"] == [], "nothing outside the group is read for a new project"
    assert all(member["original_absent"] for member in plan["candidate_value"]["cards"])
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=alice)
    c = await world.card(replace(p, access_id=identity.control_id, grantor_subject=identity.project_subject))
    assert p is not None and p.control_card is None, "P is the root"
    assert c.control_card.control_id == p.access_id and c.control_card.control_revision == p.card_revision, \
        "C is born bound to P at P's committed revision"
    census = await world.census(alice)
    _assert_complete(census[alice], control=c, ancestors=[p])
    assert census[alice]["my"]["state"] == "present"


@pytest.mark.asyncio
async def test_b3_a_genesis_planned_again_is_refused_as_existing_without_any_write(tmp_path):
    world = _World(tmp_path)
    await _genesis(world)
    before = world.files()
    alice = PEOPLE["alice"]
    again = await world.plan([_my_request(alice), _c_request(alice, {"ref": "p"}), _p_request()])
    assert again == {"ok": False, "error": "card_plan_target_exists", "status": 409}
    assert world.files() == before


@pytest.mark.asyncio
async def test_b3_join_reads_the_live_p_at_its_revision_and_commits_a_complete_chain(tmp_path):
    world = _World(tmp_path)
    _, p = await _genesis(world)
    bob = PEOPLE["bob"]
    plan = await world.plan([_c_request(bob, _live_p(p)), _my_request(bob)])
    assert plan["ok"] is True, plan
    assert plan["plan"]["reads"] == [{"subject_hash": subject_hash_for(CREATOR), "access_id": p.access_id,
                                      "revision": p.card_revision}]
    assert (await world.commit_plan(plan["plan"]))["state"] == "committed"
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=bob)
    c = await world.card(replace(p, access_id=identity.control_id, grantor_subject=identity.project_subject))
    census = await world.census(bob, PEOPLE["alice"])
    _assert_complete(census[bob], control=c, ancestors=[p])
    assert census[PEOPLE["alice"]]["chain"]["state"] == "complete", "the first person is untouched"


@pytest.mark.asyncio
async def test_b3_a_join_whose_live_p_moved_after_planning_is_refused_and_writes_nothing(tmp_path):
    world = _World(tmp_path)
    _, p = await _genesis(world)
    bob = PEOPLE["bob"]
    plan = await world.plan([_c_request(bob, _live_p(p)), _my_request(bob)])
    assert plan["ok"] is True, plan
    moved = replace(p, card_revision=p.card_revision + 1, label="P edited after planning")
    await world.service.commit(moved, subject_hash=subject_hash_for(CREATOR),
                               expected_revision=p.card_revision, now=NOW)
    try:
        result = await world.commit_plan(plan["plan"])
    except Exception as exc:  # staging raises the named refusal
        result = {"raised": type(exc).__name__, "reason": getattr(exc, "reason", str(exc))}
    assert result == {"raised": "CardTransactionRefused", "reason": "card_dependency_moved"}, result
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=bob)
    assert await world.card(replace(p, access_id=identity.control_id,
                                    grantor_subject=identity.project_subject)) is None
    census = await world.census(bob)
    assert census[bob]["control"]["state"] == "absent" and census[bob]["chain"]["state"] == "missing", census


@pytest.mark.asyncio
async def test_b3_redemption_creates_c_and_my_and_revokes_the_pending_invitation_in_one_group(tmp_path):
    world = _World(tmp_path)
    _, p = await _genesis(world)
    carol = PEOPLE["carol"]
    invitation = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="b3-invite-carol", target_email="carol@example.test")
    pending = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=invitation.project_subject, catalog_version=p.catalog_version,
        control_id=invitation.control_id, issuer_ref=invitation.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=NOW), identity=invitation)
    await world.service.commit(pending, subject_hash=subject_hash_for(pending.grantor_subject),
                               expected_revision=0, now=NOW)
    plan = await world.plan(
        [_c_request(carol, _live_p(p)), _my_request(carol)],
        [{"kind": "revoke", "target_subject": invitation.invitation_ref, "access_id": pending.access_id,
          "subject_hash": subject_hash_for(pending.grantor_subject), "original_revision": pending.card_revision}])
    assert plan["ok"] is True, plan
    assert (await world.commit_plan(plan["plan"]))["state"] == "committed"
    revoked = await world.card(pending)
    assert revoked.state == CARD_STATE_REVOKED and revoked.card_revision == pending.card_revision + 1
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=carol)
    c = await world.card(replace(p, access_id=identity.control_id, grantor_subject=identity.project_subject))
    _assert_complete((await world.census(carol))[carol], control=c, ancestors=[p])


@pytest.mark.asyncio
async def test_b3_repair_attaches_an_unbound_c_to_p_and_leaves_my_untouched(tmp_path):
    world = _World(tmp_path)
    _, p = await _genesis(world)
    dave = PEOPLE["dave"]
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=dave)
    c = build_project_person_control(identity=identity, catalog_version=p.catalog_version,
                                     actor_subject=CREATOR, request_id="b3-legacy", now=NOW)
    await world.service.commit(c, subject_hash=subject_hash_for(c.grantor_subject), expected_revision=0, now=NOW)
    my = new_project_person_my_card(control_card=c, now=NOW)
    await world.service.commit(my, subject_hash=subject_hash_for(my.grantor_subject), expected_revision=0, now=NOW)
    census = await world.census(dave)
    _assert_complete(census[dave], control=c, ancestors=[])  # before repair the chain ends at C
    plan = await world.plan([], [{"kind": "attach", "target_subject": dave, "access_id": c.access_id,
                                  "subject_hash": subject_hash_for(c.grantor_subject),
                                  "original_revision": c.card_revision, "parent": _live_p(p)}])
    assert plan["ok"] is True, plan
    assert (await world.commit_plan(plan["plan"]))["state"] == "committed"
    repaired = await world.card(c)
    assert repaired.card_revision == c.card_revision + 1 and repaired.control_card.control_id == p.access_id
    assert (await world.card(my)).to_dict() == my.to_dict(), "My is never rewritten"
    _assert_complete((await world.census(dave))[dave], control=repaired, ancestors=[p])


# --- slice 2: catalog selections resolved by the production AutomationAccessService ---

def _real_service_world(tmp_path):
    """The planner host is a real AutomationAccessService over the real Card store."""
    from test_resident_profile_cards import _Harness
    world = _World(tmp_path)
    harness = _Harness(tmp_path / "service")
    harness.service._persistence = world.persistence
    world.host = harness.service
    return world, harness


MEMORIES_READ = {"resource_grants": {"https://host/api/mcp/memories*": ["memories:read"]},
                 "resource_operations": {"https://host/api/mcp/memories*": ["search"]}}
MEMORIES_ALL = {"resource_grants": {"https://host/api/mcp/memories*": ["memories:read", "memories:write"]},
                "resource_operations": {"https://host/api/mcp/memories*": ["search", "write"]}}
DELEGABLE = ("memories:read", "memories:write")


@pytest.mark.asyncio
async def test_b3_genesis_selections_are_resolved_by_the_real_service_and_stored_exactly(tmp_path):
    from connection_hub.delegated_credentials.controls.snapshot import control_snapshot_is_exact

    world, harness = _real_service_world(tmp_path)
    alice = PEOPLE["alice"]
    resource = "https://host/api/mcp/memories*"
    p_request = {**_p_request(), "selection": MEMORIES_ALL}
    c_request = {**_c_request(alice, {"ref": "p"}), "selection": MEMORIES_READ}
    plan = await world.plan([_my_request(alice), c_request, p_request],
                            grants={"p": DELEGABLE, "c": DELEGABLE, "my": DELEGABLE})
    assert plan["ok"] is True, plan
    assert plan["plan"]["catalog_digest"], "the plan names the active catalog it resolved against"
    assert (await world.commit_plan(plan["plan"]))["state"] == "committed"
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=alice)
    p = next(CardAuthority.from_mapping(m["candidate"]) for m in plan["plan"]["candidate_value"]["cards"]
             if m["candidate"]["issuer_kind"] == "application")
    stored_p = await world.card(p)
    stored_c = await world.card(replace(p, access_id=identity.control_id, grantor_subject=identity.project_subject))
    assert stored_p.resource_operations == {resource: ("search", "write")}
    assert stored_c.resource_operations == {resource: ("search",)}
    assert stored_c.resource_grants == {resource: ("memories:read",)}
    assert stored_c.catalog_version == harness.catalog.active.version
    assert control_snapshot_is_exact(stored_c) and control_snapshot_is_exact(stored_p)
    _assert_complete((await world.census(alice))[alice], control=stored_c, ancestors=[stored_p])


@pytest.mark.asyncio
async def test_b3_a_step_cannot_select_beyond_its_own_delegable_grants(tmp_path):
    world, _harness = _real_service_world(tmp_path)
    alice = PEOPLE["alice"]
    p_request = {**_p_request(), "selection": MEMORIES_ALL}
    c_request = {**_c_request(alice, {"ref": "p"}), "selection": MEMORIES_ALL}
    before = world.files()
    # P may delegate both grants; C's own step only memories:read.
    plan = await world.plan([_my_request(alice), c_request, p_request],
                            grants={"p": DELEGABLE, "c": ("memories:read",), "my": DELEGABLE})
    assert plan["ok"] is False, plan
    assert plan["error"] == "delegated_access_grants_not_delegable", plan
    assert world.files() == before, "a refused plan writes nothing"

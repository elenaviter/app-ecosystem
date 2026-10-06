# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W594: one read-only proposal for several qualified Card changes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import (
    build_application_control,
    build_project_person_control,
    plan_card_lifecycle,
)
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED, CardAuthority
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
)
from test_card_service import _Cache
from test_card_transaction_store import Decisions


PROJECT = "work:project:quickstart"
CREATOR = "creator-1"
TARGET = "person-1"
REQUEST = "test-plan-1"


def _decision(target: str = TARGET, operation: str = PROJECT_PERSON_CONTROL_CREATE):
    return ProjectAuthorizationDecision.allow(ProjectAuthorizationRequest.build(
        actor_subject=CREATOR, project_ref=PROJECT, target_subject=target,
        operation=operation, request_id=REQUEST,
    ), delegable_grants=("work:admin",), platform_admin=True)


class _Cards:
    def __init__(self, *authorities: CardAuthority) -> None:
        self.current = {(subject_hash_for(card.grantor_subject), card.access_id): card
                        for card in authorities}
        self.reads: list[tuple[str, str]] = []

    async def load_current(self, access_id: str, *, subject_hash: str):
        self.reads.append((subject_hash, access_id))
        card = self.current.get((subject_hash, access_id))
        return (card, object()) if card is not None else None


class _Host:
    def __init__(self, *authorities: CardAuthority) -> None:
        self.cards = _Cards(*authorities)
        self.catalog = CatalogDocument.build({"delegated_credentials": {"oauth": {"resources": []}}})
        self.writes = 0

    def _cards(self):
        return self.cards

    async def _active_catalog(self):
        return self.catalog

    @staticmethod
    def _version_of(active):
        return active.version


def _p() -> CardAuthority:
    return build_application_control(
        project_ref=PROJECT, holder_subject=CREATOR, catalog_version="catalog-v1",
        issuer_label="Project", now=100,
    )


def _c(parent: CardAuthority | None = None) -> CardAuthority:
    return build_project_person_control(
        identity=ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=TARGET),
        catalog_version="catalog-v1", actor_subject=CREATOR, request_id=REQUEST,
        parent=parent, now=100,
    )


def _p_request(ref: str = "p") -> dict:
    return {"ref": ref, "kind": "application_control",
            "identity": {"holder_subject": CREATOR, "issuer_ref": PROJECT},
            "selection": {}, "parent": None}


def _c_request(parent: dict, ref: str = "c") -> dict:
    return {"ref": ref, "kind": "project_person_control",
            "identity": {"target_subject": TARGET}, "selection": {}, "parent": parent}


def _my_request(ref: str = "c") -> dict:
    return {"ref": "my", "kind": "project_person_my_card",
            "identity": {"person_subject": TARGET}, "selection": {}, "parent": {"ref": ref}}


@pytest.mark.asyncio
async def test_genesis_plans_p_c_my_without_a_live_parent_or_any_write():
    host = _Host()
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT,
        creations=[_my_request(), _c_request({"ref": "p"}), _p_request()],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result["ok"] is True, result
    plan = result["plan"]
    assert plan["candidate_value"]["schema"] == "connection-hub.card-group.v1"
    assert len(plan["candidate_value"]["cards"]) == 3
    assert all(member["original_absent"] for member in plan["candidate_value"]["cards"])
    assert plan["reads"] == []
    assert plan["participant_input"]["dependency_revisions"]
    validate_group_candidate(plan["candidate_value"], reads=plan["reads"])
    assert host.writes == 0
    assert len(host.cards.reads) == 3  # absent targets are checked, not dependency reads


@pytest.mark.asyncio
async def test_join_uses_a_present_live_p_at_its_exact_revision():
    p = _p()
    host = _Host(p)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT,
        creations=[_c_request({"access_id": p.access_id, "holder_subject": CREATOR}), _my_request()],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result["ok"] is True, result
    assert len(result["plan"]["candidate_value"]["cards"]) == 2
    assert result["plan"]["reads"] == [{
        "subject_hash": subject_hash_for(CREATOR), "access_id": p.access_id,
        "revision": p.card_revision,
    }]
    assert host.writes == 0


@pytest.mark.asyncio
async def test_stale_revision_refuses_without_a_write():
    c = _c()
    host = _Host(c)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT, creations=[], updates=[{
            "action": "revoke", "access_id": c.access_id,
            "subject_hash": subject_hash_for(c.grantor_subject),
            "original_revision": c.card_revision + 1,
        }], actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result == {"ok": False, "error": "card_plan_original_revision_changed", "status": 409}
    assert host.writes == 0
    assert host.cards.current[(subject_hash_for(c.grantor_subject), c.access_id)] is c


@pytest.mark.asyncio
async def test_redemption_proposes_new_c_and_my_with_pending_invitation_revoke():
    p = _p()
    invitation = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invite-1", target_email="person@example.test",
    )
    pending = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=invitation.project_subject, catalog_version="catalog-v1",
        control_id=invitation.control_id, issuer_ref=invitation.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=100,
    ), identity=invitation)
    host = _Host(p, pending)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT,
        creations=[_c_request({"access_id": p.access_id, "holder_subject": CREATOR}), _my_request()],
        updates=[{"action": "revoke", "access_id": pending.access_id,
                  "subject_hash": subject_hash_for(pending.grantor_subject),
                  "original_revision": pending.card_revision}],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result["ok"] is True, result
    members = result["plan"]["candidate_value"]["cards"]
    assert len(members) == 3
    revoked = next(member for member in members if member["access_id"] == pending.access_id)
    assert revoked["original_revision"] == pending.card_revision
    assert revoked["candidate"]["state"] == CARD_STATE_REVOKED
    assert result["plan"]["reads"] == [{
        "subject_hash": subject_hash_for(CREATOR), "access_id": p.access_id,
        "revision": p.card_revision,
    }]
    assert host.writes == 0


@pytest.mark.asyncio
async def test_repair_attaches_existing_c_to_current_p_without_rewriting_my():
    p, c = _p(), _c()
    host = _Host(p, c)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT, creations=[],
        updates=[{"action": "attach", "access_id": c.access_id,
                  "subject_hash": subject_hash_for(c.grantor_subject),
                  "original_revision": c.card_revision,
                  "parent": {"access_id": p.access_id, "holder_subject": CREATOR}}],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result["ok"] is True, result
    members = result["plan"]["candidate_value"]["cards"]
    assert len(members) == 1
    assert members[0]["original_revision"] == c.card_revision
    assert members[0]["candidate"]["card_revision"] == c.card_revision + 1
    assert members[0]["candidate"]["control_card"]["control_id"] == p.access_id
    assert result["plan"]["reads"] == [{
        "subject_hash": subject_hash_for(CREATOR), "access_id": p.access_id,
        "revision": p.card_revision,
    }]
    assert host.writes == 0


@pytest.mark.asyncio
async def test_join_candidate_is_exact_after_qualified_group_stage_and_recovery(tmp_path):
    p = _p()
    host = _Host(p)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT,
        creations=[_c_request({"access_id": p.access_id, "holder_subject": CREATOR}), _my_request()],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result["ok"] is True, result
    plan = result["plan"]

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield

    class _Reservations:
        def __init__(self):
            self.held = []

        async def reserve(self, **kwargs):
            self.held.append(kwargs)

        async def release(self, transaction_id, *, intent_digest):
            self.held = [item for item in self.held if item["transaction_id"] != transaction_id]

    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    reservations = _Reservations()
    tx.bind_catalog_reservations(store, reservations)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    await service.commit(p, subject_hash=subject_hash_for(CREATOR), expected_revision=0, now=100)
    members = [(member["subject_hash"], None, CardAuthority.from_mapping(member["candidate"]),
                member["action"]) for member in plan["candidate_value"]["cards"]]
    group_id, intent = "c" * 64, "d" * 64
    staged = await service.stage_group_transaction(
        transaction_id=group_id, intent_digest=intent, participant="project",
        members=members, now=datetime.fromtimestamp(100, timezone.utc),
        reads=plan["reads"], catalog=plan["catalog_digest"],
    )
    assert staged["staged"] is True
    assert reservations.held
    decisions.recorded[group_id] = "committed"
    decided = await service.decide_group_transaction(
        transaction_id=group_id, intent_digest=intent, decision="committed", now=100,
    )
    assert decided["state"] == "committed"
    for subject_hash, original, candidate, action in members:
        assert original is None and action == "create"
        stored = await store.read_current_authority(subject_hash=subject_hash, access_id=candidate.access_id)
        assert stored[1].to_dict() == candidate.to_dict()
    assert not reservations.held


@pytest.mark.asyncio
async def test_live_c_constructor_and_planner_builder_produce_the_same_record(monkeypatch):
    from test_project_person_access import (
        ADMIN, PROJECT_REF, TARGET as LIVE_TARGET, _Host as LiveHost,
        _Port, _create, _lifecycle,
    )

    monkeypatch.setattr(
        "connection_hub.delegated_credentials.project_person_access.time.time", lambda: 100,
    )
    live = LiveHost()
    result = await _create(_lifecycle(live, _Port()))
    assert result["ok"] is True, result
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=LIVE_TARGET)
    stored = live.records[(identity.project_subject, identity.control_id)][0].authority
    built = build_project_person_control(
        identity=identity, catalog_version=stored.catalog_version,
        actor_subject=ADMIN, request_id="request-create",
        label="Quickstart member", now=100, audit_at=100,
    )
    assert built.to_dict() == stored.to_dict()


@pytest.mark.asyncio
async def test_foreign_parent_and_unauthorized_target_refuse_without_writes():
    foreign = build_application_control(
        project_ref="work:project:foreign", holder_subject=CREATOR,
        catalog_version="catalog-v1", now=100,
    )
    host = _Host(foreign)
    result = await plan_card_lifecycle(
        host, project_ref=PROJECT,
        creations=[_c_request({"access_id": foreign.access_id, "holder_subject": CREATOR})],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(),
    )
    assert result == {"ok": False, "error": "project_control_not_exact", "status": 409}
    assert host.writes == 0

    result = await plan_card_lifecycle(
        _Host(), project_ref=PROJECT, creations=[_c_request({"ref": "p"}), _p_request()],
        actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
        authorization=_decision(target="different-person"),
    )
    assert result == {"ok": False, "error": "card_plan_authorization_target_mismatch", "status": 403}

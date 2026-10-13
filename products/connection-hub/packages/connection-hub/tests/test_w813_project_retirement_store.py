"""W813: P's retirement through the card_version handler on the real Card version store.

The planner is the stand-in (its output is the real ``retired_project_control`` candidate); STAGE, PUBLISH,
the replay and the stale-base refusal run on ServiceCardVersionStore over DelegatedCardService and
BundleStorageDelegatedCardStore, the same pieces W661's integration tests drive.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import (
    PROJECT_RETIREMENT_PROVENANCE,
    retired_project_control,
)
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.participant_card_version import CardVersionRefused
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_DELETE,
    LifecyclePlanAuthorizationRequest,
    LifecyclePlanStep,
    ProjectAuthorizationDecision,
    ProjectControlLocator,
)
from test_card_service import _Cache
from test_card_transaction_store import NOW, Decisions
import test_w661_card_version_operation as p2
from test_w578_card_lifecycle_plan import _p

CATALOG = p2.CATALOG


from contextlib import asynccontextmanager


@asynccontextmanager
async def _no_lock(**kwargs):
    yield


class _Catalogs:
    async def read_active(self):
        return SimpleNamespace(version=CATALOG["version"], content_hash=CATALOG["content_hash"])


class _Planner:
    def __init__(self, answer):
        self.answer = answer

    async def __call__(self, *args, **kwargs):
        return self.answer


def _decision(p, project_ref):
    request = LifecyclePlanAuthorizationRequest(
        actor_subject=p2.ACTOR if hasattr(p2, "ACTOR") else "creator-1", project_ref=project_ref,
        request_id="w813-store", request_digest="a" * 64,
        steps=(LifecyclePlanStep(ref="update:0", operation=PROJECT_DELETE, target_subject=project_ref),))
    return ProjectAuthorizationDecision.allow(
        request.step_request(request.steps[0]), delegable_grants=(), platform_admin=True,
        project_control=ProjectControlLocator(control_id=p.access_id, holder_subject=p.grantor_subject))


async def _world(tmp_path, planner_answer=None):
    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_no_lock)
    p = _p()
    subject_hash = subject_hash_for(p.grantor_subject)
    await service.commit(p, subject_hash=subject_hash, expected_revision=0, now=NOW)
    decision = _decision(p, p.issuer_ref)
    retired = retired_project_control(p, project_ref=p.issuer_ref, decision=decision,
                                      actor_subject=decision.actor_subject, request_id="w813-store", now=NOW)
    plan = planner_answer or {"ok": True, "plan": {
        "catalog_digest": catalog_version_digest(CATALOG["version"], CATALOG["content_hash"]),
        "candidate_value": {"cards": [{"subject_hash": subject_hash, "access_id": p.access_id, "action": "revoke",
                                       "original_revision": p.card_revision, "original_absent": False,
                                       "candidate": retired.to_dict()}]}}}
    card_versions = ServiceCardVersionStore(service, refused=CardVersionRefused, catalog_store=_Catalogs())
    operation, _, _ = p2._operation(planner=_Planner(plan), store=card_versions)
    store._w813_service = service
    return store, operation, p, retired, subject_hash


async def _call(operation, op, **override):
    request = p2._request(op, **override)
    return p2._verified(await operation.answer(request), request)


def _update(p, subject_hash, revision=None):
    return [{**p2.UPDATE, "kind": "retire_project_control", "target_subject": p.issuer_ref, "access_id": p.access_id,
             "subject_hash": subject_hash, "original_revision": p.card_revision if revision is None else revision}]


@pytest.mark.asyncio
async def test_p_retires_through_stage_and_publish_on_the_real_store(tmp_path):
    store, operation, p, retired, subject_hash = await _world(tmp_path)
    staged = await _call(operation, "stage", updates=_update(p, subject_hash))
    assert staged["kind"] == "staged", staged
    [member] = staged["members"]
    assert member["base_version"] == p.card_revision and member["version"] == p.card_revision + 1
    current = (await store.read_current_authority(subject_hash=subject_hash, access_id=p.access_id))[1]
    assert current == p  # not final before PUBLISH
    published = await _call(operation, "publish")
    assert published["kind"] == "published"
    current = (await store.read_current_authority(subject_hash=subject_hash, access_id=p.access_id))[1]
    assert current.state == CARD_STATE_REVOKED and current.card_revision == p.card_revision + 1
    assert current.provenance[PROJECT_RETIREMENT_PROVENANCE]["project_ref"] == p.issuer_ref
    assert not tx.card_version_marker_path(store, p2.TXN).exists()


@pytest.mark.asyncio
async def test_a_replay_after_publish_answers_already_applied(tmp_path):
    store, operation, p, retired, subject_hash = await _world(tmp_path, planner_answer={
        "ok": False, "error": "card_plan_already_applied", "status": 409})
    replay = await _call(operation, "stage", updates=_update(p, subject_hash))
    assert replay == {"kind": "already_applied"}


@pytest.mark.asyncio
async def test_a_stale_base_is_refused_at_stage(tmp_path):
    store, operation, p, retired, subject_hash = await _world(tmp_path)
    # Another write moves P before this retirement stages: the exact base fence refuses.
    service = store._w813_service
    import dataclasses
    moved = dataclasses.replace(p, card_revision=p.card_revision + 1, label="moved")
    await service.commit(moved, subject_hash=subject_hash, expected_revision=p.card_revision, now=NOW)
    refused = await _call(operation, "stage", updates=_update(p, subject_hash))
    assert refused["kind"] == "refused" and refused["code"] == "card_changed", refused


@pytest.mark.asyncio
async def test_a_lost_publish_answer_recovers_forward_and_p_stays_revoked(tmp_path):
    """codex-main (W805): P's PUBLISH answer is lost after the durable pointer move. OUTCOME with STAGE's links
    shows P published and current at its new version; a repeated PUBLISH writes nothing (txn_unknown); nothing
    is compensated, no new request is needed, and an unrelated Card is unchanged."""
    store, operation, p, retired, subject_hash = await _world(tmp_path)
    service = store._w813_service
    from test_w578_card_lifecycle_plan import _c

    unrelated = _c(p)
    unrelated_hash = subject_hash_for(unrelated.grantor_subject)
    await service.commit(unrelated, subject_hash=unrelated_hash, expected_revision=0, now=NOW)

    staged = await _call(operation, "stage", updates=_update(p, subject_hash))
    assert staged["kind"] == "staged"
    await _call(operation, "publish")  # the answer is lost: PB never reads it
    outcome = await _call(operation, "outcome", links=staged["members"])
    [member] = outcome["members"]
    assert outcome["kind"] == "outcome" and outcome["state"] == "published"
    assert member["current"] is True and member["version"] == p.card_revision + 1
    # The same PUBLISH again finds no marker (it was consumed): PB reads OUTCOME, never re-stages or compensates.
    assert await _call(operation, "publish") == {"kind": "refused", "code": "txn_unknown", "status": 404}
    current = (await store.read_current_authority(subject_hash=subject_hash, access_id=p.access_id))[1]
    assert current.state == CARD_STATE_REVOKED and current.card_revision == p.card_revision + 1
    assert (await store.read_current_authority(subject_hash=unrelated_hash,
                                               access_id=unrelated.access_id))[1] == unrelated

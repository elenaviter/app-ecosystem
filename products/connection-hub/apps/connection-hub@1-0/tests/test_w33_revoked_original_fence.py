"""W33: re-consent after a revoked OAuth Card, through the real Hub service, durable Card store and host fence.

The Hub service plans a re-consent on top of the revoked revision and commits the next one (W571,
test_w571_a_revoked_oauth_card_reconsented_continues_its_revision_chain). The host's original-exchange fence
reads the same durable Card store. Before W33 its pending branch also required the original to be active, so
every re-consent after a revoke was refused with original_exchange_target_moved. Service-only and mocked-host
tests could not show that composition; this test runs both halves on one real store, with card transactions,
PostgreSQL and Redis (CONNECTION_HUB_TEST_POSTGRES_DSN and REDIS_URL; skipped without them).
"""
from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_host_composition import _load_entrypoint_module

PENDING = SimpleNamespace(state="pending")
COMMITTED = SimpleNamespace(state="committed")


@pytest.fixture
def host():
    entrypoint = _load_entrypoint_module()
    return importlib.import_module(entrypoint.__package__ + ".surfaces.oauth_original_exchange_host")


@pytest.fixture
def w603(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "packages/connection-hub/tests"))
    return importlib.import_module("test_w603_original_issuance")


def _target(host, w):
    # The entrypoint binds the Hub service, its OAuth issuance store and the durable Card store the same way.
    return host.OriginalTarget(tenant=w.authority.tenant, project=w.authority.project, hub=w.service,
                               issuance_store=w.authority, cards=w.store)


async def _revoked_card(w603, w):
    first = await w603._begin(w)
    await w603._reserve(w, first)
    await w.service.complete_oauth_issuance(transaction_id=first.transaction_id)
    user = {"user_id": w603.GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}
    assert (await w.service.revoke_access(user, access_id=first.access_id)).get("ok"), "revoke"
    revoked = await w603._card(w, first.access_id)
    assert revoked.state == "revoked"
    return first, revoked


@pytest.mark.asyncio
async def test_re_consent_after_a_revoke_passes_every_host_fence_and_commits_the_next_revision(tmp_path, host, w603):
    async with w603._world(tmp_path) as w:
        first, revoked = await _revoked_card(w603, w)
        target = _target(host, w)
        plan = await w603._begin(w, request="exchange-after-revoke")
        assert plan.access_id == first.access_id and plan.base_revision == revoked.card_revision

        original, candidate = await target.candidate(plan)
        assert original.state == "revoked" and original.card_revision == revoked.card_revision
        assert candidate.state == "active" and candidate.card_revision == plan.candidate_revision
        # The SDK fences before preparing the pair (original_code_flow.py): this refused before W33.
        assert await target.fence(plan=plan, result=PENDING) is True
        # The committed fence does not take the revoked original for the delivery target.
        assert await target.fence(plan=plan, result=COMMITTED) is False

        await w603._reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "committed" and result.card_revision == plan.candidate_revision
        current = await w603._card(w, plan.access_id)
        assert current.state == "active" and current.card_revision == plan.candidate_revision
        # The SDK fences again before activation and before publication: the exact active candidate.
        assert await target.fence(plan=plan, result=COMMITTED) is True
        # The id never restarts; the chain only moved forward from the revoked revision.
        assert current.access_id == first.access_id and current.card_revision > revoked.card_revision


@pytest.mark.asyncio
async def test_a_revoked_card_changed_after_the_plan_is_still_refused(tmp_path, host, w603):
    """A successor written after the plan (here a relabel of the revoked Card) moves the target."""
    async with w603._world(tmp_path) as w:
        _, revoked = await _revoked_card(w603, w)
        target = _target(host, w)
        plan = await w603._begin(w, request="exchange-after-revoke")
        assert await target.fence(plan=plan, result=PENDING) is True
        await w.cards.commit(replace(revoked, label="changed after the plan", card_revision=revoked.card_revision + 1),
                             subject_hash=w.subject_hash, expected_revision=revoked.card_revision, now=0)
        moved = await w603._card(w, plan.access_id)
        assert moved.card_revision == revoked.card_revision + 1
        assert await target.fence(plan=plan, result=PENDING) is False

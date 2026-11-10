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


# The full SDK path: OriginalCodeExchangeFlow behind /token, the actual Hub decision, real PostgreSQL and Redis,
# with the host's OriginalTarget as its fence (the SDK fixture's own fence is a revision-only stub).
from kdcube_ai_app.infra.secrets.tests.test_runtime_http import rig as secrets_rig  # noqa: E402,F401 (fixture)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.tests.original_hub_fixture import (  # noqa: E402
    RESOURCE, hub_world, token_digests,  # noqa: F401 (hub_world is a fixture)
)


async def _authorize_again(r, verifier):
    """A fresh authorization code for the same client and resource, as a new consent produces one."""
    from starlette.datastructures import FormData
    from connection_hub.delegated_credentials.oauth.pkce import make_s256_challenge
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_exchange import CodeExchangeProof
    code = await r.grants.create_auth_code(client_id=r.client, redirect_uri="https://unit.test/cb",
        code_challenge=make_s256_challenge(verifier), sub=r.subject, scopes=["records:read"], resource=RESOURCE)
    r.proof = CodeExchangeProof.from_request(tenant=r.tenant, project=r.project, code=code,
        client_id=r.client, redirect_uri="https://unit.test/cb", verifier=verifier)

    async def form():
        return FormData({"grant_type": "authorization_code", "code": code, "client_id": r.client,
                         "redirect_uri": "https://unit.test/cb", "code_verifier": verifier})
    r.request.form = form


@pytest.mark.asyncio
async def test_sdk_token_exchange_after_a_revoke_issues_a_new_family_on_the_next_revision(hub_world, host):
    import json
    from connection_hub.delegated_credentials.cards.store import subject_hash_for
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.http import routes
    r = hub_world
    target = host.OriginalTarget(tenant=r.tenant, project=r.project, hub=r.hub, issuance_store=r.oauth, cards=r.cards)
    r.flow.fence_target = target.fence

    async def card(access_id):
        found = await r.cards.read_current_authority(subject_hash=subject_hash_for(r.subject), access_id=access_id)
        return None if found is None else found[1]

    first = await routes.token(r.request)
    assert first.status_code == 200, r.errors
    first_refresh = json.loads(first.body)["refresh_token"]
    access_id = (await r.ledger.read(r.proof)).plan["access_id"]
    issued = await card(access_id)
    assert issued.state == "active"
    assert await r.oauth.get_refresh_token_state(first_refresh) is not None

    user = {"user_id": r.subject, "roles": ["kdcube:role:registered"], "permissions": []}
    assert (await r.hub.revoke_access(user, access_id=access_id)).get("ok"), "revoke"
    revoked = await card(access_id)
    assert revoked.state == "revoked"
    assert await r.oauth.get_refresh_token_state(first_refresh) is None

    await _authorize_again(r, "y" * 48)
    second = await routes.token(r.request)
    # Before W33 the host fence refused here: original_exchange_target_moved.
    assert second.status_code == 200, r.errors
    plan = (await r.ledger.read(r.proof)).plan
    assert plan["access_id"] == access_id and plan["base_revision"] == revoked.card_revision
    current = await card(access_id)
    assert current.state == "active" and current.card_revision == plan["candidate_revision"] > revoked.card_revision
    second_refresh = json.loads(second.body)["refresh_token"]
    # A new refresh family is live; the revoked one is not revived.
    assert await r.oauth.get_refresh_token_state(second_refresh) is not None
    assert await r.oauth.get_refresh_token_state(first_refresh) is None
    assert token_digests(second) != token_digests(first)
    # One refresh of the new family: it rotates at the Card's new incarnation; the revoked family does not.
    state = await r.oauth.get_refresh_token_state(second_refresh)
    rotated = await r.oauth.rotate_refresh_token(second_refresh, dict(state.record), ttl_seconds=3600,
                                                 card_incarnation=current.card_revision)
    assert rotated and await r.oauth.get_refresh_token_state(rotated) is not None
    assert await r.oauth.rotate_refresh_token(first_refresh, dict(state.record), ttl_seconds=3600,
                                              card_incarnation=current.card_revision) is None

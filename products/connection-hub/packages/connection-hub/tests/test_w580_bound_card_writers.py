"""Every writer of a bound Card meets the W578 transaction fence (W580).

A project transaction stages a bound Card's next revision. Until PB records
its decision, no ordinary writer may change that Card or anything that serves
it: the durable Card, its credential handles, the grant store's credential
lifetimes, invocation policies, and the Control state.

The Card store, the transaction participant and the credential handles are the
production implementations (handles on the Redis named by ``REDIS_URL``).
Only the serving projection and the grant store's credential lifetimes are
fakes, and the grant store records every call so a refused write can be shown
to have touched nothing.
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.credential_handles import (
    RedisCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    CardStorageError,
    subject_hash_for,
)
from test_caller_writer_gate import _card
from test_card_service import _Cache
from test_card_transaction_store import INTENT, TX, Decisions
from test_project_control_cards import _Redis

pytestmark = pytest.mark.skipif(
    not os.environ.get("REDIS_URL"),
    reason="REDIS_URL is not set; the real credential handles are skipped",
)

ACCESS_TOKEN = "handle-access-token"


class _Grants:
    """The grant store's credential lifetimes; records every call it gets."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.before_extend = None

    async def extend_card_credentials(self, access_id: str, ttl: int) -> bool:
        if self.before_extend is not None:
            await self.before_extend()
        self.calls.append(("extend_card_credentials", access_id, ttl))
        return True


@pytest.fixture
async def redis_client():
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Redis at REDIS_URL is unreachable: {exc}")
    yield client
    await client.aclose()


async def _bound(tmp_path, redis_client, *, source="manual", bound=True):
    """A committed Card, its real handles, and a service over the real store."""

    now = int(time.time())
    card = dataclasses.replace(
        _card(bound=bound, revision=1), source=source, created_at=now - 60, expires_at=now + 3600
    )
    if source == "oauth":
        card = dataclasses.replace(card, card_kind=CARD_KIND_CONNECTOR)
    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield

    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    subject_hash = subject_hash_for(card.grantor_subject)
    await cards.commit(card, subject_hash=subject_hash, expected_revision=0, now=now)
    namespace = uuid.uuid4().hex[:8]
    handles = RedisCardCredentialHandleStore(
        redis_client, tenant=f"t-{namespace}", project=f"p-{namespace}"
    )
    held = CardCredentialHandles(access_id=card.access_id, access_token=ACCESS_TOKEN)
    await handles.write(card, held)
    persistence = DurableCardPersistence(
        redis=redis_client, tenant=f"t-{namespace}", project=f"p-{namespace}",
        card_store=store, mutation_lock=mutation_lock, credential_handles=handles,
    )
    # Only the serving projection is fake, as in test_revoke_target_revision.
    persistence._cards = cards
    grants = _Grants()
    service = AutomationAccessService(
        redis=_Redis(), tenant="tenant", project="project", config=None,
        grant_store=grants, card_persistence=persistence,
    )
    service.notify_change = AsyncMock()
    return SimpleNamespace(
        card=card, store=store, cards=cards, handles=handles, held=held, grants=grants,
        service=service, subject_hash=subject_hash, now=now,
        user={"user_id": card.grantor_subject},
    )


async def _stage(f) -> None:
    candidate = dataclasses.replace(f.card, card_revision=f.card.card_revision + 1, label="staged")
    await tx.stage(
        f.store, transaction_id=TX, intent_digest=INTENT, participant="project",
        subject_hash=f.subject_hash, original=f.card, candidate=candidate,
        now=datetime.fromtimestamp(f.now, timezone.utc),
    )


async def _current(f):
    return (await f.store.read_current_authority(
        subject_hash=f.subject_hash, access_id=f.card.access_id))[1]


async def _abort(f) -> None:
    f.store._card_transaction_decisions.recorded[TX] = "aborted"
    await tx.decide(f.store, transaction_id=TX, intent_digest=INTENT, decision="aborted")


async def _assert_untouched(f) -> None:
    """Nothing serving the Card moved, read back from the real stores."""

    with pytest.raises(CardStorageError, match="card_transaction_undecided"):
        await _current(f)
    assert await f.handles.read(f.card) == f.held
    assert f.grants.calls == []
    f.service.notify_change.assert_not_called()
    await _abort(f)
    assert await _current(f) == f.card


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prolong", "reissue"])
async def test_renew_of_a_staged_bound_card_is_refused_with_no_effect(tmp_path, redis_client, mode):
    f = await _bound(tmp_path, redis_client)
    await _stage(f)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode=mode)
    assert result["ok"] is False and result["retryable"] is True
    assert result["error"] == "delegated_cards_unavailable"
    await _assert_untouched(f)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="W580 N1: a stage after the precondition read is refused at commit, but the "
    "credential extension is not undone; closes with one shared SQL transaction",
)
async def test_a_prolong_staged_after_its_load_extends_no_credential(tmp_path, redis_client):
    f = await _bound(tmp_path, redis_client, source="oauth")
    # The project stages the Card after renew loaded it, before the credential moves.
    f.grants.before_extend = lambda: _stage(f)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong")
    assert result["ok"] is False and result["retryable"] is True
    await _assert_untouched(f)


@pytest.mark.asyncio
async def test_renew_of_an_unbound_card_without_a_transaction_still_prolongs(tmp_path, redis_client):
    f = await _bound(tmp_path, redis_client, source="oauth", bound=False)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong")
    assert result["ok"] is True and result["mode"] == "prolong"
    current = await _current(f)
    assert current.card_revision == f.card.card_revision + 1
    assert current.expires_at > f.card.expires_at
    assert [call[0] for call in f.grants.calls] == ["extend_card_credentials"]
    f.service.notify_change.assert_awaited_once()


# Writers driven through the resident-profile harness: catalog, grant store,
# minter and a real InvocationPolicyService on bundle storage, with the Card
# persistence swapped for the transactional one above.


async def _hub(tmp_path, redis_client):
    from test_resident_profile_cards import _Harness

    h = _Harness(tmp_path)
    store = BundleStorageDelegatedCardStore(tmp_path / "cards")
    tx.bind_transaction_decisions(store, Decisions())

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield

    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    namespace = uuid.uuid4().hex[:8]
    handles = RedisCardCredentialHandleStore(
        redis_client, tenant=f"t-{namespace}", project=f"p-{namespace}"
    )
    persistence = DurableCardPersistence(
        redis=redis_client, tenant=f"t-{namespace}", project=f"p-{namespace}",
        card_store=store, mutation_lock=mutation_lock, credential_handles=handles,
    )
    persistence._cards = cards
    h.service._persistence = persistence
    h.store, h.cards, h.handles, h.now = store, cards, handles, int(time.time())
    return h


async def _seed(h, card):
    from test_resident_profile_cards import GRANTOR

    held = CardCredentialHandles(access_id=card.access_id, access_token=f"old-{card.access_id}")
    await h.cards.commit(card, subject_hash=subject_hash_for(GRANTOR), expected_revision=0, now=h.now)
    await h.handles.write(card, held)
    return held


async def _stage_on(h, card) -> None:
    from test_resident_profile_cards import GRANTOR

    candidate = dataclasses.replace(card, card_revision=card.card_revision + 1, label="staged")
    await tx.stage(
        h.store, transaction_id=TX, intent_digest=INTENT, participant="project",
        subject_hash=subject_hash_for(GRANTOR), original=card, candidate=candidate,
        now=datetime.fromtimestamp(h.now, timezone.utc),
    )


async def _decide(h, decision: str) -> None:
    h.store._card_transaction_decisions.recorded[TX] = decision
    await tx.decide(h.store, transaction_id=TX, intent_digest=INTENT, decision=decision)


async def _read(h, card):
    from test_resident_profile_cards import GRANTOR

    return (await h.store.read_current_authority(
        subject_hash=subject_hash_for(GRANTOR), access_id=card.access_id))[1]


def _scoped_card(h):
    from test_resident_profile_cards import MEMORIES

    card = h.legacy_authority(
        resource=MEMORIES, grants=("memories:read",), operations=("search",),
        created_at=h.now - 60, expires_at=h.now + 3600,
        account_scope={"google": {"acct-1": ("mail:read",)}},
    )
    return dataclasses.replace(card, card_revision=1)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="W580 finding 2: prune skips a staged Card silently (pruned=0); the "
    "disconnected account stays bound after the transaction commits",
)
async def test_a_disconnect_never_leaves_its_account_on_a_staged_card(tmp_path, redis_client):
    from test_resident_profile_cards import GRANTOR

    h = await _hub(tmp_path, redis_client)
    card = _scoped_card(h)
    await _seed(h, card)
    await _stage_on(h, card)
    result = await h.service.prune_account_from_grants(
        grantor_subject=GRANTOR, provider_id="google", account_id="acct-1"
    )
    await _decide(h, "committed")
    # Either the prune is refused loudly for a retry, or the account is gone.
    assert result.get("ok") is False or "acct-1" not in (await _read(h, card)).account_scope.get("google", {})


@pytest.mark.asyncio
async def test_prune_of_a_card_without_a_transaction_still_clears_the_account(tmp_path, redis_client):
    from test_resident_profile_cards import GRANTOR

    h = await _hub(tmp_path, redis_client)
    card = _scoped_card(h)
    await _seed(h, card)
    result = await h.service.prune_account_from_grants(
        grantor_subject=GRANTOR, provider_id="google", account_id="acct-1"
    )
    assert result == {"pruned": 1, "grants": [card.access_id]}
    current = await _read(h, card)
    assert "google" not in current.account_scope
    assert current.card_revision == card.card_revision + 1


async def _fold_setup(h):
    from connection_hub.invocation_policy import POLICY_ALWAYS, SURFACE_OUTER, InvocationAuthority
    from test_resident_profile_cards import GRANTOR, MAIL, MEMORIES, _profile

    legacy = dataclasses.replace(h.legacy_authority(
        resource=MEMORIES, grants=("memories:read",), operations=("search",),
        created_at=h.now - 5000, expires_at=h.now + 40_000), card_revision=1)
    stable = dataclasses.replace(h.legacy_authority(
        resource=MAIL, grants=("mail:read",), operations=("search",),
        created_at=h.now - 100, expires_at=h.now + 30_000),
        access_id=_profile().access_id, card_revision=1)
    held = {card.access_id: await _seed(h, card) for card in (legacy, stable)}
    await h.policies.set_policy(
        owner_subject=GRANTOR, mode=POLICY_ALWAYS,
        authority=InvocationAuthority(access_id=legacy.access_id, resource=MEMORIES,
                                      surface=SURFACE_OUTER, operation="search"),
    )
    moved = InvocationAuthority(access_id=stable.access_id, resource=MEMORIES,
                                surface=SURFACE_OUTER, operation="search")
    return legacy, stable, held, moved


async def _fold_refused_with_no_effect(h, legacy, stable, held, moved, result):
    from test_resident_profile_cards import GRANTOR

    assert result["ok"] is False and result["retryable"] is True
    assert await h.policies.get(owner_subject=GRANTOR, authority=moved) is None
    assert h.grant_store.bindings == {} and h.grant_store.revoked == []
    for card in (legacy, stable):
        assert await h.handles.read(card) == held[card.access_id]
    await _decide(h, "aborted")
    assert await _read(h, stable) == stable
    assert await _read(h, legacy) == legacy


@pytest.mark.asyncio
async def test_a_fold_into_a_staged_stable_card_is_refused_with_no_effect(tmp_path, redis_client):
    from test_resident_profile_cards import CLIENT, USER

    h = await _hub(tmp_path, redis_client)
    legacy, stable, held, moved = await _fold_setup(h)
    await _stage_on(h, stable)
    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    await _fold_refused_with_no_effect(h, legacy, stable, held, moved, result)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="W580 finding 3: a fold staged after its precondition read binds a minted "
    "grant and writes the moved invocation policy before the refused commit",
)
async def test_a_fold_staged_after_its_precondition_moves_no_policy_or_grant(tmp_path, redis_client):
    from test_resident_profile_cards import CLIENT, USER

    h = await _hub(tmp_path, redis_client)
    legacy, stable, held, moved = await _fold_setup(h)
    mint = h.service._mint_card_credential

    async def staged_then_mint(*args, **kwargs):
        await _stage_on(h, stable)
        return await mint(*args, **kwargs)

    h.service._mint_card_credential = staged_then_mint
    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    await _fold_refused_with_no_effect(h, legacy, stable, held, moved, result)

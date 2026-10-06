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


async def _hub(tmp_path, redis_client, *, connections=None):
    from test_resident_profile_cards import _Harness

    h = _Harness(tmp_path, connections=connections)
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
async def test_a_prune_of_a_staged_card_is_refused_loudly_for_a_retry(tmp_path, redis_client):
    """W580 finding 2, package half. The disconnect route must honour this
    refusal; that end-to-end order is an app hunk outside this package."""

    from test_resident_profile_cards import GRANTOR

    h = await _hub(tmp_path, redis_client)
    card = _scoped_card(h)
    await _seed(h, card)
    await _stage_on(h, card)
    result = await h.service.prune_account_from_grants(
        grantor_subject=GRANTOR, provider_id="google", account_id="acct-1"
    )
    assert result["ok"] is False and result["retryable"] is True and result["pruned"] == 0
    # A staged Card makes the listing itself unreadable; a per-Card refusal names it.
    assert result["reason"] in {"grants_unreadable", "account_binding_not_pruned"}
    if result["reason"] == "account_binding_not_pruned":
        assert result["not_pruned"] == [card.access_id]
    await _decide(h, "aborted")
    assert await _read(h, card) == card


@pytest.mark.asyncio
async def test_prune_of_a_card_without_a_transaction_still_clears_the_account(tmp_path, redis_client):
    from test_resident_profile_cards import GRANTOR

    h = await _hub(tmp_path, redis_client)
    card = _scoped_card(h)
    await _seed(h, card)
    result = await h.service.prune_account_from_grants(
        grantor_subject=GRANTOR, provider_id="google", account_id="acct-1"
    )
    assert result["ok"] is True and result["not_pruned"] == []
    assert result["pruned"] == 1 and result["grants"] == [card.access_id]
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


def _stage_before(h, method: str, card) -> None:
    """The project stages ``card`` just before the writer's ``method`` runs."""

    original = getattr(h.service, method)

    async def staged_then_call(*args, **kwargs):
        await _stage_on(h, card)
        return await original(*args, **kwargs)

    setattr(h.service, method, staged_then_call)


async def _stable_card(h):
    from test_resident_profile_cards import MEMORIES, _profile

    stable = dataclasses.replace(h.legacy_authority(
        resource=MEMORIES, grants=("memories:read",), operations=("search",),
        created_at=h.now - 100, expires_at=h.now + 30_000),
        access_id=_profile().access_id, card_revision=1)
    return stable, await _seed(h, stable)


async def _consent(h):
    from test_resident_profile_cards import CLIENT, TASKS, USER

    return await h.service.create_access(
        USER, label="", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search"]}, client_id=CLIENT,
    )


async def _refused_untouched(h, card, held, result):
    assert result["ok"] is False and result["retryable"] is True
    assert h.grant_store.bindings == {} and h.grant_store.revoked == []
    assert await h.handles.read(card) == held
    await _decide(h, "aborted")
    assert await _read(h, card) == card


@pytest.mark.asyncio
async def test_a_consent_merging_into_a_staged_card_is_refused_with_no_effect(tmp_path, redis_client):
    h = await _hub(tmp_path, redis_client)
    stable, held = await _stable_card(h)
    await _stage_on(h, stable)
    await _refused_untouched(h, stable, held, await _consent(h))


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="W580 finding 3 (create_access): a consent staged after its precondition "
    "read binds a minted grant before the refused commit",
)
async def test_a_consent_staged_after_its_precondition_binds_no_grant(tmp_path, redis_client):
    h = await _hub(tmp_path, redis_client)
    stable, held = await _stable_card(h)
    _stage_before(h, "_mint_card_credential", stable)
    await _refused_untouched(h, stable, held, await _consent(h))


@pytest.mark.asyncio
async def test_a_consent_on_a_card_without_a_transaction_still_merges(tmp_path, redis_client):
    from test_resident_profile_cards import TASKS

    h = await _hub(tmp_path, redis_client)
    stable, _held = await _stable_card(h)
    result = await _consent(h)
    assert result["ok"] is True and result["access"]["access_id"] == stable.access_id
    current = await _read(h, stable)
    assert current.card_revision == stable.card_revision + 1
    assert TASKS in current.resource_grants
    assert [binding["registry_access_id"] for binding in h.grant_store.bindings.values()] == [
        stable.access_id
    ]


async def _manual_card(h):
    from test_resident_profile_cards import USER

    created = await h.service.create_access(
        USER, label="manual", resource_grants={_memories(): ["memories:read"]},
        resource_operations={_memories(): ["search"]},
    )
    assert created["ok"], created
    access_id = created["access"]["access_id"]
    card = await _read(h, SimpleNamespace(access_id=access_id))
    h.grant_store.bindings.clear()
    return card, await h.handles.read(card)


def _memories():
    from test_resident_profile_cards import MEMORIES

    return MEMORIES


@pytest.mark.asyncio
async def test_a_reissue_of_a_staged_card_is_refused_with_no_effect(tmp_path, redis_client):
    from test_resident_profile_cards import USER

    h = await _hub(tmp_path, redis_client)
    card, held = await _manual_card(h)
    await _stage_on(h, card)
    result = await h.service.renew_access(USER, access_id=card.access_id, mode="reissue")
    await _refused_untouched(h, card, held, result)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="W580 finding 3 (renew reissue): a reissue staged after its precondition "
    "read binds a minted grant before the refused commit",
)
async def test_a_reissue_staged_after_its_precondition_binds_no_grant(tmp_path, redis_client):
    from test_resident_profile_cards import USER

    h = await _hub(tmp_path, redis_client)
    card, held = await _manual_card(h)
    _stage_before(h, "_mint_card_credential", card)
    result = await h.service.renew_access(USER, access_id=card.access_id, mode="reissue")
    await _refused_untouched(h, card, held, result)


async def _oauth_card(h):
    from test_oauth_card_extension import _issued

    record = await _issued(h)
    card = await _read(h, record)
    return card, await h.handles.read(card)


async def _extend(h, card):
    from test_oauth_card_extension import CLIENT
    from test_resident_profile_cards import USER

    return await h.service.extend_client_access(
        USER, client_id=CLIENT, access_id=card.access_id,
        resource=_memories(), claims=["memories:write"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after_precondition"])
async def test_an_extension_of_a_staged_oauth_card_is_refused_with_no_effect(
    tmp_path, redis_client, stage
):
    h = await _hub(tmp_path, redis_client)
    card, held = await _oauth_card(h)
    if stage == "before":
        await _stage_on(h, card)
    else:
        # Staged after the card loaded, before the extension commits.
        _stage_before(h, "_resolve_card_authority", card)
    await _refused_untouched(h, card, held, await _extend(h, card))


@pytest.mark.asyncio
async def test_an_extension_of_an_oauth_card_without_a_transaction_still_extends(tmp_path, redis_client):
    h = await _hub(tmp_path, redis_client)
    card, _held = await _oauth_card(h)
    result = await _extend(h, card)
    assert result["ok"] is True, result
    current = await _read(h, card)
    assert current.card_revision == card.card_revision + 1
    assert "memories:write" in current.resource_grants[_memories()]


async def _rotate(h):
    from test_oauth_card_extension import CLIENT, CONCRETE
    from test_resident_profile_cards import GRANTOR

    return await h.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Claude Code",
        scopes=["memories:read"], resource=CONCRETE,
        access_token="at-2", refresh_token="rt-2",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after_precondition"])
async def test_a_token_rotation_on_a_staged_oauth_card_is_refused_with_no_effect(
    tmp_path, redis_client, stage
):
    from connection_hub.delegated_credentials.cards.service import CardConflict

    h = await _hub(tmp_path, redis_client)
    card, held = await _oauth_card(h)
    if stage == "before":
        await _stage_on(h, card)
    else:
        _stage_before(h, "_load_record", card)
    # The token route catches CardConflict and withholds or keeps its tokens.
    # An already-staged Card is refused at its read, with the generic reason.
    reason = "delegated_cards_unavailable" if stage == "before" else "card_transaction_unresolved"
    with pytest.raises(CardConflict, match=reason):
        await _rotate(h)
    assert await h.handles.read(card) == held
    await _decide(h, "aborted")
    assert await _read(h, card) == card


@pytest.mark.asyncio
async def test_a_token_rotation_without_a_transaction_still_records(tmp_path, redis_client):
    h = await _hub(tmp_path, redis_client)
    card, _held = await _oauth_card(h)
    recorded = await _rotate(h)
    assert recorded is not None and recorded.access_id == card.access_id
    assert (await _read(h, card)).card_revision == card.card_revision + 1
    assert (await h.handles.read(card)).access_token == "at-2"


# The issuer's Control Card: a start from a profile, and the legacy snapshot
# migration that control_card_create runs on an existing Control.


async def _control(h, **changes):
    from test_resident_profile_cards import USER

    made = await h.service.control_card_create(
        USER, issuer_ref="work:project:one", issuer_kind="application", **changes
    )
    return made


async def _empty_control(tmp_path, redis_client):
    from test_resident_profile_cards import _connections_with_authorization_profiles

    h = await _hub(tmp_path, redis_client, connections=_connections_with_authorization_profiles())
    made = await _control(h)
    assert made["ok"] is True and made["started_from"] == {}
    card = await _read(h, SimpleNamespace(access_id=made["authority"]["access_id"]))
    return h, card


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after_precondition"])
async def test_starting_a_staged_control_from_a_profile_is_refused_with_no_effect(
    tmp_path, redis_client, stage
):
    h, control = await _empty_control(tmp_path, redis_client)
    if stage == "before":
        await _stage_on(h, control)
    else:
        # Staged after the Control loaded, before its start commits.
        _stage_before(h, "control_card_update", control)
    result = await _control(h, initial_profile="coordinator")
    assert result["ok"] is False and result.get("retryable") is True, result
    assert h.grant_store.bindings == {}
    await _decide(h, "aborted")
    assert await _read(h, control) == control


@pytest.mark.asyncio
async def test_starting_a_control_without_a_transaction_still_starts(tmp_path, redis_client):
    h, control = await _empty_control(tmp_path, redis_client)
    result = await _control(h, initial_profile="coordinator")
    assert result["ok"] is True and result["started"] is True, result
    current = await _read(h, control)
    assert current.card_revision > control.card_revision
    assert any(current.resource_grants.values())

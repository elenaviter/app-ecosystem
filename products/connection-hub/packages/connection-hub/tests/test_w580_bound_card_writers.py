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


# A bound Card with no transaction: each governed writer is decided by its
# binding's policy (W502 B). A refusal changes nothing that serves the Card.


class _Policy:
    def __init__(self, allow: bool) -> None:
        self.allow, self.calls = allow, []

    def _decision(self, request):
        from datetime import timedelta
        from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteDecision

        return CallerWriteDecision(self.allow, "" if self.allow else "pb_refused", "policy:v1",
                                   datetime.now(timezone.utc) + timedelta(minutes=5), request)

    async def decide(self, request):
        self.calls.append(("decide", request.action))
        return self._decision(request)

    async def revalidate(self, request, initial):
        self.calls.append(("revalidate", request.action))
        return self._decision(request)

    async def finalize(self, request, *, state, card_revision):
        self.calls.append(("finalize", state))
        return True


async def _bind_policy(h, card, allow):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding
    from test_resident_profile_cards import GRANTOR

    bound = dataclasses.replace(card, card_revision=card.card_revision + 1, control_card=ControlCardBinding(
        control_id="control-1", issuer_ref="work:project:one", issuer_kind="project", control_revision=1))
    await h.cards.commit(bound, subject_hash=subject_hash_for(GRANTOR),
                         expected_revision=card.card_revision, now=h.now)
    policy = _Policy(allow)
    registry = CallerWriterRegistry()
    registry.register("project", policy)
    h.service.bind_caller_writers(registry)
    h.grant_store.bindings.clear()
    return bound, policy


async def _bound_writer(h, writer):
    from test_resident_profile_cards import GRANTOR, USER

    if writer == "consent":
        card, _ = await _stable_card(h)
        return card, lambda: _consent(h)
    if writer == "extend":
        card, _ = await _oauth_card(h)
        return card, lambda: _extend(h, card)
    if writer == "fold":
        from test_resident_profile_cards import CLIENT

        _legacy, stable, _held, h.moved = await _fold_setup(h)
        return stable, lambda: h.service.migrate_resident_profile(USER, client_id=CLIENT)
    if writer == "prune":
        card = _scoped_card(h)
        await _seed(h, card)
        return card, lambda: h.service.prune_account_from_grants(
            grantor_subject=GRANTOR, provider_id="google", account_id="acct-1")
    card, _ = await _manual_card(h)
    return card, lambda: h.service.renew_access(USER, access_id=card.access_id, mode="reissue")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["consent", "extend", "prune", "reissue", "fold"])
async def test_a_bound_writer_commits_only_what_its_binding_policy_allows(tmp_path, redis_client, writer):
    h = await _hub(tmp_path, redis_client)
    card, call = await _bound_writer(h, writer)
    bound, policy = await _bind_policy(h, card, allow=True)
    result = await call()
    assert result["ok"] is True, result
    assert [c for c in policy.calls if c[0] == "decide"], policy.calls
    assert ("finalize", "committed") in policy.calls
    assert (await _read(h, bound)).card_revision == bound.card_revision + 1


_MINTS_BEFORE_POLICY = pytest.mark.xfail(
    strict=True,
    reason="W580 finding 4: the writer mints and binds a live grant before its binding "
    "policy decides, so a policy refusal (no transaction at all) leaves the binding",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", [
    pytest.param("consent", marks=_MINTS_BEFORE_POLICY), "extend", "prune",
    pytest.param("reissue", marks=_MINTS_BEFORE_POLICY),
    pytest.param("fold", marks=_MINTS_BEFORE_POLICY),
])
async def test_a_bound_writer_refused_by_its_policy_changes_nothing(tmp_path, redis_client, writer):
    h = await _hub(tmp_path, redis_client)
    card, call = await _bound_writer(h, writer)
    bound, policy = await _bind_policy(h, card, allow=False)
    held = await h.handles.read(bound)
    result = await call()
    assert result["ok"] is False, result
    assert [c for c in policy.calls if c[0] == "decide"], policy.calls
    assert await _read(h, bound) == bound
    assert await h.handles.read(bound) == held
    assert h.grant_store.bindings == {}, "a refused write left a live grant binding"
    if writer == "fold":
        from test_resident_profile_cards import GRANTOR

        assert await h.policies.get(owner_subject=GRANTOR, authority=h.moved) is None


# C negatives the hub enforces itself, on a bound OAuth Card's prolongation:
# a missing, refusing, expired or mismatched policy decision, and a candidate
# that would move expiry backward. Whether a prolongation stays within the
# Control's own validity is the binding policy's decision (PB), not the hub's.


class _Answer(_Policy):
    def __init__(self, mode: str) -> None:
        super().__init__(allow=mode != "refuse")
        self.mode = mode

    def _decision(self, request):
        from datetime import timedelta
        from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteDecision

        if self.mode == "expired":
            return CallerWriteDecision(True, "", "policy:v1", datetime.now(timezone.utc) - timedelta(seconds=1), request)
        if self.mode == "mismatch":
            other = dataclasses.replace(request, access_id="another-card")
            return CallerWriteDecision(True, "", "policy:v1", datetime.now(timezone.utc) + timedelta(minutes=5), other)
        return super()._decision(request)


def _bind_answer(f, mode: str):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry

    registry = CallerWriterRegistry()
    policy = _Answer(mode)
    if mode == "missing":
        registry.require("project")  # bound kind known, no policy registered
    else:
        registry.register("project", policy)
    f.service.bind_caller_writers(registry)
    return policy


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [
    ("missing", "caller_writer_policy_unavailable"),
    ("refuse", "pb_refused"),
    ("expired", "caller_writer_decision_expired"),
    ("mismatch", "caller_writer_decision_mismatch"),
])
async def test_a_bound_prolong_without_a_valid_decision_is_refused(tmp_path, redis_client, mode, reason):
    f = await _bound(tmp_path, redis_client, source="oauth")
    _bind_answer(f, mode)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong")
    assert result["ok"] is False and result["error"] == reason, result
    assert await _current(f) == f.card
    assert await f.handles.read(f.card) == f.held
    f.service.notify_change.assert_not_called()


@pytest.mark.asyncio
async def test_a_bound_prolong_may_not_move_expiry_backward(tmp_path, redis_client):
    f = await _bound(tmp_path, redis_client, source="oauth")
    policy = _bind_answer(f, "allow")
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=60)
    assert result["ok"] is False and result["error"] == "caller_writer_prolong_shape_invalid", result
    assert [c for c in policy.calls if c[0] == "decide"] == [], "the shape is checked before the policy is asked"
    assert await _current(f) == f.card


@pytest.mark.asyncio
async def test_a_bound_prolong_with_a_valid_decision_extends_forward(tmp_path, redis_client):
    f = await _bound(tmp_path, redis_client, source="oauth")
    policy = _bind_answer(f, "allow")
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=7200)
    assert result["ok"] is True, result
    assert ("decide", "prolong") in policy.calls and ("finalize", "committed") in policy.calls
    current = await _current(f)
    assert current.expires_at > f.card.expires_at and current.card_revision == f.card.card_revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,ttl", [
    ("missing", None), ("refuse", None), ("expired", None), ("mismatch", None), ("allow", 60),
])
async def test_a_refused_bound_prolong_extends_no_credential(tmp_path, redis_client, mode, ttl):
    """W580 finding 5, fixed in W578 91eaa0d1: the policy decides before any extension."""

    f = await _bound(tmp_path, redis_client, source="oauth")
    _bind_answer(f, mode)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=ttl)
    assert result["ok"] is False, result
    assert f.grants.calls == [], "a refused prolong extended the credential"


@pytest.mark.parametrize("scope,refusal", [
    ({"google": {"acct-1": ["mail:read"]}}, None),
    ({}, None),
    ({"google": {"acct-1": ["mail:read"], "acct-2": ["mail:read"]}}, "caller_writer_prune_shape_invalid"),
    ({"google": {"acct-1": ["mail:read", "mail:send"]}}, "caller_writer_prune_shape_invalid"),
])
def test_a_prune_may_only_narrow_account_bindings(scope, refusal):
    from connection_hub.delegated_credentials.caller_writer_gate import candidate_shape_refusal

    before = _card(bound=True, revision=3).to_dict()
    before["account_scope"] = {"google": {"acct-1": ["mail:read"], "acct-3": ["mail:read"]}}
    candidate = dict(before, account_scope=scope, card_revision=4)
    assert candidate_shape_refusal("prune", before, candidate) == refusal
    # Anything other than the account scope is outside a prune.
    widened = dict(candidate, label="renamed")
    assert candidate_shape_refusal("prune", before, widened) == "caller_writer_prune_shape_invalid"


# T4 (Ops, W578 eb522266): with the generic coordinator bound, a bound prolong
# never calls extend_*; its lifetime is a recorded effect of the one decision.


def _coordinate(f):
    # The generic coordinator is W581's (service_foundation.coordination); W578's
    # coordinated path imports it too, so without it there is nothing to drive.
    pytest.importorskip("service_foundation.coordination", reason="W581 coordinator not on the path")
    from service_foundation.coordination.durable_decision_log import Coordinator
    from connection_hub.delegated_credentials.cards.card_participant import (
        PARTICIPANT, DecisionStorePort, HubCardParticipant, LocalCardIntentSource,
    )
    from test_card_participant import _Store, _Verifier

    decisions = _Store()
    tx.bind_transaction_decisions(f.store, DecisionStorePort(decisions))
    intents = LocalCardIntentSource(f.store)
    hub = HubCardParticipant(service=f.cards, store=f.store, intents=intents, decisions=decisions)
    f.service.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, _Verifier()),
                                    intents=intents, decisions=decisions)
    return decisions


class _Extends(_Grants):
    """Every lifetime call the grant store offers, each recorded."""

    live = True

    async def card_credentials_live(self, access_id):
        # Read-only (W578 79e0bd5b): not an extension, so not recorded.
        return self.live

    async def extend_refresh_token(self, token, ttl):
        self.calls.append(("extend_refresh_token", ttl))
        return True

    async def extend_access_grant(self, token, ttl):
        self.calls.append(("extend_access_grant", ttl))
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["allow", "refuse"])
async def test_a_coordinated_bound_prolong_calls_no_extend_before_or_after_its_decision(
    tmp_path, redis_client, mode
):
    f = await _bound(tmp_path, redis_client, source="oauth")
    f.grants = f.service._store = _Extends()
    decisions = _coordinate(f)
    policy = _bind_answer(f, mode)
    applied = []

    async def apply(kind, key, payload, *, transaction_id):
        applied.append((kind, key, dict(payload)))

    f.cards.bind_effect_applier(apply)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=7200)
    assert f.grants.calls == [], "the coordinated prolong called extend_* directly"
    if mode == "allow":
        assert result["ok"] is True, result
        assert decisions.decisions == ["committed"]
        assert ("finalize", "committed") in policy.calls
        current = await _current(f)
        assert current.card_revision == f.card.card_revision + 1 and current.expires_at > f.card.expires_at
        # The lifetime moves only as the decision's effect, to the Card's absolute expiry.
        assert [(kind, payload["expires_at"]) for kind, _key, payload in applied] == [
            ("credential_lifetime", current.expires_at)
        ]
    else:
        assert result["ok"] is False and result["error"] == "pb_refused", result
        assert decisions.decisions in ([], ["aborted"])
        assert await _current(f) == f.card
        assert applied == []
    assert await tx.list_in_doubt(f.store) == []


@pytest.mark.asyncio
async def test_a_coordinated_prolong_staged_after_its_load_extends_nothing(tmp_path, redis_client):
    """F1/N1 on the coordinated path: another transaction stages the Card after
    the prolong loaded it; the prolong's own prepare is refused, nothing moves."""

    f = await _bound(tmp_path, redis_client, source="oauth")
    f.grants = f.service._store = _Extends()
    _coordinate(f)
    _bind_answer(f, "allow")
    applied = []

    async def apply(kind, key, payload, *, transaction_id):
        applied.append(kind)

    f.cards.bind_effect_applier(apply)
    coordinated_write = f.service._coordinated_write

    async def staged_then_write(*args, **kwargs):
        await _stage(f)
        return await coordinated_write(*args, **kwargs)

    f.service._coordinated_write = staged_then_write
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=7200)
    assert result["ok"] is False and result["retryable"] is True, result
    assert f.grants.calls == [] and applied == []
    assert await f.handles.read(f.card) == f.held


@pytest.mark.asyncio
async def test_a_coordinated_prolong_of_an_ended_credential_is_refused_before_any_stage(tmp_path, redis_client):
    f = await _bound(tmp_path, redis_client, source="oauth")
    f.grants = f.service._store = _Extends()
    f.grants.live = False
    decisions = _coordinate(f)
    policy = _bind_answer(f, "allow")
    applied = []

    async def apply(kind, key, payload, *, transaction_id):
        applied.append(kind)

    f.cards.bind_effect_applier(apply)
    result = await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=7200)
    assert result["ok"] is False and result["error"] == "delegated_access_credential_expired", result
    assert decisions.decisions == [] and applied == [] and f.grants.calls == []
    assert await _current(f) == f.card
    assert await tx.list_in_doubt(f.store) == []


@pytest.mark.asyncio
async def test_an_ended_credential_prolong_opens_no_policy_decision(tmp_path, redis_client):
    """W580 finding 6, fixed in W578 3751e64a: liveness is checked before the policy is asked."""

    f = await _bound(tmp_path, redis_client, source="oauth")
    f.grants = f.service._store = _Extends()
    f.grants.live = False
    _coordinate(f)
    policy = _bind_answer(f, "allow")
    await f.service.renew_access(f.user, access_id=f.card.access_id, mode="prolong", ttl_seconds=7200)
    assert policy.calls == [], policy.calls


async def _legacy_control(tmp_path, redis_client):
    """A Control whose snapshot property is gone, so asking for it migrates it."""

    from connection_hub.delegated_credentials.controls.snapshot import (
        CONTROL_SNAPSHOT_PROPERTY, control_snapshot_is_exact,
    )
    from test_resident_profile_cards import GRANTOR

    h, control = await _empty_control(tmp_path, redis_client)
    properties = dict(control.properties or {})
    properties.pop(CONTROL_SNAPSHOT_PROPERTY, None)
    legacy = dataclasses.replace(control, card_revision=control.card_revision + 1, properties=properties)
    await h.cards.commit(legacy, subject_hash=subject_hash_for(GRANTOR),
                         expected_revision=control.card_revision, now=h.now)
    assert not control_snapshot_is_exact(legacy)
    return h, legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after_load"])
async def test_a_snapshot_migration_of_a_staged_control_is_refused_with_no_effect(tmp_path, redis_client, stage):
    h, legacy = await _legacy_control(tmp_path, redis_client)
    if stage == "before":
        await _stage_on(h, legacy)
    else:
        _stage_before(h, "_ensure_control_snapshot", legacy)
    result = await _control(h)
    assert result["ok"] is False and result.get("retryable") is True, result
    await _decide(h, "aborted")
    assert await _read(h, legacy) == legacy


@pytest.mark.asyncio
async def test_a_snapshot_migration_without_a_transaction_still_migrates(tmp_path, redis_client):
    from connection_hub.delegated_credentials.controls.snapshot import control_snapshot_is_exact

    h, legacy = await _legacy_control(tmp_path, redis_client)
    result = await _control(h)
    assert result["ok"] is True and result["created"] is False, result
    current = await _read(h, legacy)
    assert current.card_revision == legacy.card_revision + 1 and control_snapshot_is_exact(current)


@pytest.mark.asyncio
async def test_a_fold_without_a_binding_or_transaction_still_folds(tmp_path, redis_client):
    from test_resident_profile_cards import CLIENT, GRANTOR, MAIL, MEMORIES, USER

    h = await _hub(tmp_path, redis_client)
    legacy, stable, _held, moved = await _fold_setup(h)
    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    assert result["ok"] is True and result["folded"] == [legacy.access_id], result
    current = await _read(h, stable)
    assert current.card_revision == stable.card_revision + 1 and current.control_card is None
    assert set(current.resource_grants) == {MEMORIES, MAIL}
    assert (await h.policies.get(owner_subject=GRANTOR, authority=moved)).mode == "always"
    assert (await _read(h, legacy)).state != "active"
    assert [b["registry_access_id"] for b in h.grant_store.bindings.values()] == [stable.access_id]


# Ops review of 2f66d439 (G1-G3): the remaining cells of the writer matrix.


@pytest.mark.asyncio
async def test_a_prune_staged_after_its_listing_is_refused_loudly(tmp_path, redis_client):
    """G1: the transaction starts after prune listed the Card, before its commit."""

    from test_resident_profile_cards import GRANTOR

    h = await _hub(tmp_path, redis_client)
    card = _scoped_card(h)
    held = await _seed(h, card)
    _stage_before(h, "_persist_record", card)
    result = await h.service.prune_account_from_grants(
        grantor_subject=GRANTOR, provider_id="google", account_id="acct-1"
    )
    assert result["ok"] is False and result["retryable"] is True, result
    assert result["reason"] == "account_binding_not_pruned" and result["not_pruned"] == [card.access_id]
    assert result["pruned"] == 0
    assert await h.handles.read(card) == held
    await _decide(h, "aborted")
    assert await _read(h, card) == card


@pytest.mark.asyncio
async def test_a_reissue_of_a_card_without_a_binding_or_transaction_still_reissues(tmp_path, redis_client):
    """G2: the ordinary manual reissue is unchanged."""

    from test_resident_profile_cards import USER

    h = await _hub(tmp_path, redis_client)
    card, held = await _manual_card(h)
    result = await h.service.renew_access(USER, access_id=card.access_id, mode="reissue")
    assert result["ok"] is True, result
    current = await _read(h, card)
    assert current.card_revision == card.card_revision + 1 and current.control_card is None
    # A manual bearer is not retained; the reissued session and its binding are new.
    assert (await h.handles.read(card)).session_id not in ("", held.session_id)
    assert [b["registry_access_id"] for b in h.grant_store.bindings.values()] == [card.access_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_a_bound_token_rotation_is_decided_by_its_binding_policy(tmp_path, redis_client, allow):
    """G3: record_oauth_grant names oauth_grant; a refusal raises and records nothing."""

    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused

    h = await _hub(tmp_path, redis_client)
    card, _held = await _oauth_card(h)
    bound, policy = await _bind_policy(h, card, allow=allow)
    held = await h.handles.read(bound)
    if allow:
        recorded = await _rotate(h)
        assert recorded is not None and recorded.access_id == bound.access_id
        assert ("decide", "oauth_grant") in policy.calls and ("finalize", "committed") in policy.calls
        current = await _read(h, bound)
        assert current.card_revision == bound.card_revision + 1
        assert (await h.handles.read(bound)).access_token == "at-2"
    else:
        with pytest.raises(CallerWriteRefused, match="pb_refused"):
            await _rotate(h)
        assert ("decide", "oauth_grant") in policy.calls
        assert await _read(h, bound) == bound
        assert await h.handles.read(bound) == held


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_a_bound_control_start_is_decided_by_its_binding_policy(tmp_path, redis_client, allow):
    """G3: starting a bound Control from a profile is governed; a refusal changes nothing."""

    h, control = await _empty_control(tmp_path, redis_client)
    bound, policy = await _bind_policy(h, control, allow=allow)
    result = await _control(h, initial_profile="coordinator")
    assert [c for c in policy.calls if c[0] == "decide"], policy.calls
    if allow:
        assert result["ok"] is True and result["started"] is True, result
        assert ("finalize", "committed") in policy.calls
        current = await _read(h, bound)
        assert current.card_revision == bound.card_revision + 1 and any(current.resource_grants.values())
    else:
        assert result["ok"] is False, result
        assert await _read(h, bound) == bound

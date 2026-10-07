"""W603: an authorization-code exchange's Card and credentials under ONE Card decision.

Real: the Card store (bundle files), the Card service, the Hub participant,
the Coordinator, the PostgreSQL decision store, the PostgreSQL OAuth authority
(plans, reservations, families, generations, access bindings), the effect
applier with the credential_issue target and the Redis credential handles.
Fake: only the serving projection (``_Cache``). The SDK side is played by the
test: it mints synthetic bearers, builds the same non-secret records the SDK's
GrantStore writes, and passes the Hub only their SHA-256 digests.

DSN- and Redis-gated (CONNECTION_HUB_TEST_POSTGRES_DSN, REDIS_URL).
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.credential_handles import RedisCardCredentialHandleStore
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.oauth.authority import build_delegated_client_credential
from connection_hub.delegated_credentials.oauth.authority_store import PostgresOAuthAuthorityStore
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256
from connection_hub.delegated_credentials.oauth.store import GrantStore
from connection_hub.delegated_credentials.oauth_issuance import (
    ISSUANCE_DELIVERY_SECONDS, IssuanceRefused, OAuthIssuancePlan,
)
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
from test_card_service import _Cache
from test_project_control_cards import _Redis
from test_resident_profile_cards import _Catalog, _connections

GRANTOR = "user-w603"
CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"
RESOURCE = "https://host/api/mcp/memories"
SCOPES = ["memories:read"]


@asynccontextmanager
async def _world(tmp_path):
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn or not os.environ.get("REDIS_URL"):
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN and REDIS_URL are required")
    import asyncpg
    import redis.asyncio as redis_asyncio
    from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

    redis_client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    namespace = uuid.uuid4().hex[:10]
    schema = f"hub_w603_{namespace}"
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    decisions = PostgresDecisionStore(pool, schema=schema, namespace="connection-hub-w603")
    await decisions.ensure_schema()
    authority = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=f"t-{namespace}", project="w603")
    await authority.ensure_schema()
    grants = GrantStore(object(), tenant=authority.tenant, project=authority.project, authority_store=authority)

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    handles = RedisCardCredentialHandleStore(redis_client, tenant=authority.tenant, project=authority.project)
    persistence = DurableCardPersistence(redis=redis_client, tenant=authority.tenant, project=authority.project,
                                         card_store=store, mutation_lock=mutation_lock, credential_handles=handles)
    persistence._cards = cards  # only the serving projection is fake
    compose_card_effects(card_service=cards, card_store=store, grant_store=grants, policies=None,
                         issuance_store=authority, credential_handles=handles)
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=cards, store=store, intents=intents, decisions=decisions)
    connections = _connections()
    service = AutomationAccessService(redis=_Redis(), tenant=authority.tenant, project=authority.project,
                                      config=oauth_delegated_config_from_connections(connections),
                                      catalog_resolver=_Catalog(connections), grant_store=grants,
                                      card_persistence=persistence)
    service.notify_change = AsyncMock()
    service.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, HubLocalReceiptVerifier(store)),
                                  intents=intents, decisions=decisions, intent_ttl_seconds=60)
    service.bind_oauth_issuance_store(authority)
    try:
        yield SimpleNamespace(service=service, store=store, cards=cards, authority=authority, decisions=decisions,
                              pool=pool, schema=schema, subject_hash=subject_hash_for(GRANTOR))
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
        await pool.close()
        await redis_client.aclose()


async def _begin(w, request: str = "exchange-1", **changes):
    values = dict(grantor_subject=GRANTOR, client_id=CLIENT, original_request_id=request, client_label="Claude Code",
                  scopes=SCOPES, resource=RESOURCE)
    values.update(changes)
    return await w.service.begin_oauth_issuance(**values)


def _records(w, plan: OAuthIssuancePlan, **credential_changes):
    """The non-secret records the SDK's GrantStore writes for each slot."""
    credential = build_delegated_client_credential(
        grantor_subject=GRANTOR, client_id=CLIENT, scopes=SCOPES, tenant=w.authority.tenant,
        project=w.authority.project, expires_in=3600, resources=[RESOURCE]).to_dict()
    credential.update(credential_changes)
    return {"access": {"operations": [], "resource_grants": {}, "resource_operations": {}, "credential": credential,
                       "grantor_authority": {}, "delegation_edges": [], "named_services": {},
                       "registry_access_id": plan.access_id},
            "refresh": {"registry_access_id": plan.access_id, "card_kind": "", "client_id": CLIENT, "sub": GRANTOR,
                        "scopes": SCOPES, "operations": [], "resource_grants": {}, "resource_operations": {},
                        "resource": RESOURCE, "identity_scope": "", "credential": credential}}


async def _reserve(w, plan, slots=("access", "refresh")):
    tokens = {slot: secrets.token_urlsafe(32) for slot in slots}
    records = _records(w, plan)
    for slot in slots:
        await w.service.reserve_oauth_issuance(plan=plan, slot=slot, token_sha256=bearer_sha256(tokens[slot]),
                                               record=records[slot], ttl_seconds=3600)
    return tokens


async def _usable(w, tokens) -> dict[str, bool]:
    return {"access": await w.authority.get_access_grant_record(tokens["access"]) is not None,
            "refresh": await w.authority.get_refresh_token_state(tokens["refresh"]) is not None}


async def _card(w, access_id):
    found = await w.store.read_current_authority(subject_hash=w.subject_hash, access_id=access_id)
    return None if found is None else found[1]


async def _lead_outcomes(w, plan) -> dict[str, str]:
    """The applier's own recorded outcome per effect, by slot (independent of the result object)."""
    receipt_id = tx.member_transaction_id(plan.transaction_id, 0) if plan.base_revision == 0 else plan.transaction_id
    receipt = await tx.read_receipt(w.store, receipt_id)
    outcomes = await tx.effect_outcomes(w.store, receipt_id)
    return {effect["key"]: outcomes.get(str(index), "") for index, effect in enumerate(receipt["effects"])}


@pytest.mark.asyncio
async def test_a_first_consent_creates_the_card_and_activates_its_original_credentials_in_one_decision(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        now = await w.authority.issuance_clock()
        assert (plan.base_revision, plan.candidate_revision, plan.slots) == (0, 1, ("access", "refresh"))
        assert plan.delivery_deadline == min(plan.expires_at, plan.delivery_deadline) <= now + ISSUANCE_DELIVERY_SECONDS
        assert plan.reserved_until <= plan.delivery_deadline and plan.credential_subject != GRANTOR
        assert await _card(w, plan.access_id) is None  # begun, not staged: nothing exists
        tokens = await _reserve(w, plan)
        assert await _usable(w, tokens) == {"access": False, "refresh": False}
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id,
                                                         expect={s: bearer_sha256(t) for s, t in tokens.items()})
        assert (result.state, result.card_revision, result.transaction_id, result.intent_digest) \
            == ("committed", 1, plan.transaction_id, plan.intent_digest)
        assert {slot: outcome.outcome for slot, outcome in result.per_slot.items()} \
            == {"access": "applied", "refresh": "applied"}
        assert result.receipt_digest and result.delivery_deadline == plan.delivery_deadline
        # The plan's fixed effect digests are exactly what the applier bound and recorded.
        assert await _lead_outcomes(w, plan) == dict(plan.effect_digests)
        assert await _usable(w, tokens) == {"access": True, "refresh": True}
        card = await _card(w, plan.access_id)
        assert (card.card_revision, card.expires_at, card.content_hash()) == (1, plan.expires_at,
                                                                              plan.card_content_hash)
        # The Card is readable through persistence: its handle metadata names the committed revision.
        assert (await w.service._cards().load(plan.access_id, subject_hash=w.subject_hash))[0] == card
        # A repeated completion is the same outcome and mints nothing.
        again = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert again == result
        assert await tx.list_in_doubt(w.store) == [] and await w.decisions.list_in_doubt(limit=10) == []
        w.service.notify_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_replayed_begin_returns_the_identical_plan_and_changed_inputs_refuse(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        assert await _begin(w) == plan  # deadlines included: fixed once, never recalculated
        with pytest.raises(IssuanceRefused, match="issuance_replay_changed"):
            await _begin(w, scopes=["memories:read", "memories:write"])
        with pytest.raises(IssuanceRefused, match="issuance_request_invalid"):
            await _begin(w, request=" padded ")


@pytest.mark.asyncio
async def test_reserve_trusts_only_the_transaction_id_and_refuses_a_tampered_plan_or_record(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        records = _records(w, plan)
        digest = bearer_sha256(secrets.token_urlsafe(32))
        tampered = [dataclasses.replace(plan, reserved_until=plan.reserved_until + 3600),
                    dataclasses.replace(plan, delivery_deadline=plan.delivery_deadline + 3600),
                    dataclasses.replace(plan, access_id="aut_other"),
                    dataclasses.replace(plan, credential_subject=GRANTOR),
                    dataclasses.replace(plan, effect_digests={"access": "0" * 64, "refresh": "1" * 64})]
        for changed in tampered:
            with pytest.raises(IssuanceRefused, match="issuance_plan_mismatch"):
                await w.service.reserve_oauth_issuance(plan=changed, slot="access", token_sha256=digest,
                                                       record=records["access"], ttl_seconds=3600)
        wrong = [_records(w, plan, subject=GRANTOR)["access"], _records(w, plan, tenant="other")["access"],
                 {**records["access"], "registry_access_id": "aut_other"},
                 {**records["access"], "access_token": "raw"}, {**records["refresh"], "sub": "someone-else"}]
        for record in wrong:
            with pytest.raises(IssuanceRefused, match="issuance_record_(mismatch|secret_field)"):
                await w.service.reserve_oauth_issuance(plan=plan, slot="access", token_sha256=digest, record=record,
                                                       ttl_seconds=3600)
        assert await w.authority.issuance_reservations(plan.transaction_id) == {}  # nothing written
        assert await _begin(w) == plan  # and no deadline moved


@pytest.mark.asyncio
async def test_a_missing_reservation_aborts_and_leaves_no_card_and_nothing_usable(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan, slots=("access",))
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "aborted" and result.card_revision == 0
        assert result.per_slot["refresh"].outcome == "pending" and result.per_slot["refresh"].token_sha256 == ""
        assert await _card(w, plan.access_id) is None
        assert await w.authority.get_access_grant_record(tokens["access"]) is None
        with pytest.raises(IssuanceRefused, match="issuance_decision_closed"):
            await _reserve(w, plan, slots=("refresh",))
        # The never-bound reservation ends by the sweep once its deadline passes; nothing activates it.
        assert await w.authority.issuance_reservations(plan.transaction_id) != {}


@pytest.mark.asyncio
async def test_a_reconsent_changes_the_existing_card_through_the_single_card_transaction(tmp_path):
    async with _world(tmp_path) as w:
        first = await _begin(w)
        first_tokens = await _reserve(w, first)
        await w.service.complete_oauth_issuance(transaction_id=first.transaction_id)
        plan = await _begin(w, request="exchange-2", scopes=["memories:read", "memories:write"])
        assert (plan.base_revision, plan.candidate_revision, plan.access_id) == (1, 2, first.access_id)
        tokens = await _reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert (result.state, result.card_revision) == ("committed", 2)
        assert await _lead_outcomes(w, plan) == dict(plan.effect_digests)
        assert await _usable(w, tokens) == {"access": True, "refresh": True}
        assert await _usable(w, first_tokens) == {"access": True, "refresh": True}  # today's rule: kept
        assert (await _card(w, plan.access_id)).card_revision == 2


@pytest.mark.asyncio
async def test_an_interrupted_activation_recovers_the_same_original_credentials(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan)
        real = w.authority.activate_issued_credential
        failures = {"left": 1}

        async def crash_once(**kwargs):
            if failures["left"]:
                failures["left"] -= 1
                raise ConnectionError("database connection lost")
            return await real(**kwargs)

        w.authority.activate_issued_credential = crash_once
        pending = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert pending.state == "pending" and pending.receipt_digest == ""
        assert await _usable(w, tokens) == {"access": False, "refresh": False}
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "committed"
        assert {s: o.token_sha256 for s, o in result.per_slot.items()} == {s: bearer_sha256(t) for s, t in tokens.items()}
        assert await _usable(w, tokens) == {"access": True, "refresh": True}


@pytest.mark.asyncio
async def test_a_late_activation_after_a_newer_card_with_no_family_is_superseded(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan)
        real = w.authority.activate_issued_credential

        async def lost(**_kwargs):
            raise ConnectionError("database connection lost")

        w.authority.activate_issued_credential = lost
        assert (await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)).state == "pending"
        # A newer Card of the same id is now authoritative and has no credential family at all.
        receipt = await tx.read_receipt(w.store, tx.member_transaction_id(plan.transaction_id, 0))
        from connection_hub.delegated_credentials.cards.model import CardAuthority, CardCurrentPointer
        after = CardCurrentPointer.from_mapping(receipt["after"])
        current = await w.store.read_revision(subject_hash=w.subject_hash, access_id=plan.access_id,
                                              revision_name=after.revision_name)
        newer = dataclasses.replace(current, card_revision=2, label="replacement")
        from datetime import datetime, timezone
        pointer = await w.store.write_revision(subject_hash=w.subject_hash, authority=newer,
                                               updated_at=datetime.now(timezone.utc))
        # The store itself never lets a newer Card land while these effects are pending ...
        from connection_hub.delegated_credentials.cards.store import CardStorageError
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await w.store.advance_current(subject_hash=w.subject_hash, pointer=pointer)
        # ... so an out-of-band writer forces it, to show the fence holds even then.
        from connection_hub.delegated_credentials.durable_io import write_json_atomic
        await write_json_atomic(w.store.current_path(subject_hash=w.subject_hash, access_id=plan.access_id),
                                pointer.to_dict())
        assert isinstance(newer, CardAuthority)
        w.authority.activate_issued_credential = real
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert {slot: outcome.outcome for slot, outcome in result.per_slot.items()} \
            == {"access": "superseded", "refresh": "superseded"}
        assert await _usable(w, tokens) == {"access": False, "refresh": False}
        async with w.pool.acquire() as connection:
            assert await connection.fetchval(
                f"SELECT count(*) FROM {w.authority.schema}.connection_hub_oauth_credential_families") == 0


@pytest.mark.asyncio
async def test_without_transactions_or_the_issuance_store_nothing_begins(tmp_path):
    async with _world(tmp_path) as w:
        w.service._oauth_issuance_store = None
        with pytest.raises(IssuanceRefused, match="card_transactions_unavailable"):
            await _begin(w)


@pytest.mark.asyncio
async def test_concurrent_completions_converge_on_one_committed_outcome(tmp_path):
    """A lost-response retry racing the first call (another SDK process) reads that call's outcome."""
    import asyncio

    async with _world(tmp_path) as w:
        for attempt in range(8):
            plan = await _begin(w, request=f"exchange-race-{attempt}")
            tokens = await _reserve(w, plan)
            results = await asyncio.gather(*(w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
                                             for _ in range(3)))
            states = [result.state for result in results]
            assert "aborted" not in states and states.count("committed") >= 1, (attempt, states)
            final = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
            assert final.state == "committed", (attempt, final)
            assert {slot: o.outcome for slot, o in final.per_slot.items()} == {"access": "applied",
                                                                               "refresh": "applied"}
            assert await _usable(w, tokens) == {"access": True, "refresh": True}
        async with w.pool.acquire() as connection:
            families = await connection.fetchval(
                f"SELECT count(*) FROM {w.authority.schema}.connection_hub_oauth_credential_families")
        assert families == 8  # one family per issuance: never activated twice
        assert await w.decisions.list_in_doubt(limit=20) == [] and await tx.list_in_doubt(w.store) == []


@pytest.mark.asyncio
async def test_a_replacement_card_at_the_same_revision_supersedes_the_old_reservation(tmp_path):
    """The incarnation fence: same id, revision and expiry, different content -> superseded, nothing usable."""
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan)
        real = w.authority.activate_issued_credential

        async def lost(**_kwargs):
            raise ConnectionError("database connection lost")

        w.authority.activate_issued_credential = lost
        assert (await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)).state == "pending"
        from datetime import datetime, timezone

        from connection_hub.delegated_credentials.cards.model import CardCurrentPointer
        from connection_hub.delegated_credentials.durable_io import write_json_atomic
        receipt = await tx.read_receipt(w.store, tx.member_transaction_id(plan.transaction_id, 0))
        committed = await w.store.read_revision(
            subject_hash=w.subject_hash, access_id=plan.access_id,
            revision_name=CardCurrentPointer.from_mapping(receipt["after"]).revision_name)
        replacement = dataclasses.replace(committed, label="re-created elsewhere")
        assert (replacement.card_revision, replacement.expires_at, replacement.state) \
            == (committed.card_revision, committed.expires_at, committed.state)
        assert replacement.content_hash() != committed.content_hash()
        pointer = await w.store.write_revision(subject_hash=w.subject_hash, authority=replacement,
                                               updated_at=datetime.now(timezone.utc))
        await write_json_atomic(w.store.current_path(subject_hash=w.subject_hash, access_id=plan.access_id),
                                pointer.to_dict())  # forced past the store's own fence, as an out-of-band writer
        w.authority.activate_issued_credential = real
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert {slot: o.outcome for slot, o in result.per_slot.items()} == {"access": "superseded",
                                                                           "refresh": "superseded"}
        assert await _usable(w, tokens) == {"access": False, "refresh": False}

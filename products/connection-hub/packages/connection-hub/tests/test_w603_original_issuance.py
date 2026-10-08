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
async def _world(tmp_path, *, postgres_handles: bool = False, with_policies: bool = False):
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
    metadata = None
    if postgres_handles:  # W606: the PostgreSQL handle store, whose rows bind the Card revision
        from connection_hub.delegated_credentials.authority_config import AUTHORITY_BACKEND_POSTGRESQL
        from connection_hub.delegated_credentials.cards.credential_handles import PostgresCardCredentialHandleStore
        from connection_hub.delegated_credentials.cards.handle_authority import PostgresCardHandleMetadataStore
        metadata = PostgresCardHandleMetadataStore(pg_pool=pool, tenant=authority.tenant, project=authority.project)
        await metadata.ensure_schema()
        handles = PostgresCardCredentialHandleStore(metadata_store=metadata, resident_secrets=object())
        persistence = DurableCardPersistence(redis=redis_client, tenant=authority.tenant, project=authority.project,
                                             card_store=store, mutation_lock=mutation_lock,
                                             credential_handles=handles, authority_backend=AUTHORITY_BACKEND_POSTGRESQL)
    else:
        handles = RedisCardCredentialHandleStore(redis_client, tenant=authority.tenant, project=authority.project)
        persistence = DurableCardPersistence(redis=redis_client, tenant=authority.tenant, project=authority.project,
                                             card_store=store, mutation_lock=mutation_lock, credential_handles=handles)
    persistence._cards = cards  # only the serving projection is fake
    policies = None
    if with_policies:  # W585: the real invocation policy service, on bundle storage
        from connection_hub.invocation_policy import BundleStorageInvocationPolicyStore, InvocationPolicyService

        @asynccontextmanager
        async def policy_lock(**_kwargs):
            yield {}

        policies = InvocationPolicyService(store=BundleStorageInvocationPolicyStore(tmp_path / "policies"),
                                           mutation_lock=policy_lock)
    compose_card_effects(card_service=cards, card_store=store, grant_store=grants, policies=policies,
                         issuance_store=authority, credential_handles=handles)
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=cards, store=store, intents=intents, decisions=decisions)
    connections = _connections()
    service = AutomationAccessService(redis=_Redis(), tenant=authority.tenant, project=authority.project,
                                      config=oauth_delegated_config_from_connections(connections),
                                      catalog_resolver=_Catalog(connections), grant_store=grants,
                                      card_persistence=persistence, invocation_policy_service=policies)
    service.notify_change = AsyncMock()
    service.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, HubLocalReceiptVerifier(store)),
                                  intents=intents, decisions=decisions, intent_ttl_seconds=60)
    service.bind_oauth_issuance_store(authority)
    service.bind_card_credential_handles(handles)
    try:
        yield SimpleNamespace(service=service, store=store, cards=cards, authority=authority, decisions=decisions,
                              pool=pool, schema=schema, subject_hash=subject_hash_for(GRANTOR), metadata=metadata,
                              policies=policies)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
            if metadata is not None:
                await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()
        await redis_client.aclose()


async def _begin(w, request: str = "exchange-1", **changes):
    values = dict(grantor_subject=GRANTOR, client_id=CLIENT, original_request_id=request, client_label="Claude Code",
                  scopes=SCOPES, resource=RESOURCE)
    values.update(changes)
    return await w.service.begin_oauth_issuance(**values)


def _records(w, plan: OAuthIssuancePlan, **credential_changes):
    """The non-secret records the SDK's GrantStore writes for each slot."""
    # The SDK copies the plan's authority snapshot (declared keys) into both records and the envelope.
    grants = {key: list(items) for key, items in plan.resource_grants.items()}
    operations_map = {key: list(items) for key, items in plan.resource_operations.items()}
    credential = build_delegated_client_credential(
        grantor_subject=plan.grantor_subject, client_id=plan.client_id, scopes=SCOPES, tenant=w.authority.tenant,
        project=w.authority.project, expires_in=3600, resources=list(grants), resource_grants=grants,
        resource_operations=operations_map, operations=list(plan.operations)).to_dict()
    credential.update(credential_changes)
    return {"access": {"operations": list(plan.operations), "resource_grants": grants,
                       "resource_operations": operations_map, "credential": credential,
                       "grantor_authority": {}, "delegation_edges": [], "named_services": {},
                       "registry_access_id": plan.access_id},
            "refresh": {"registry_access_id": plan.access_id, "card_kind": "", "client_id": plan.client_id,
                        "sub": plan.grantor_subject,
                        "scopes": SCOPES, "operations": list(plan.operations), "resource_grants": grants,
                        "resource_operations": operations_map,
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


@pytest.mark.asyncio
async def test_the_records_and_envelopes_must_carry_exactly_the_planned_card_authority(tmp_path):
    """Readers take operations and resource maps from these records; they are the Card's, never the SDK's own."""
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        declared = next(iter(plan.resource_grants))
        assert declared == RESOURCE + "*"  # the Card's declared key, not the concrete URL the client used
        changes = {
            "operations": ["memories.write"],
            "resource_grants": {declared: ["memories:read", "memories:write"]},
            "resource_operations": {declared: ["memories.write"]},
        }

        async def refused(slot, record):
            with pytest.raises(IssuanceRefused, match="issuance_record_authority_mismatch"):
                await w.service.reserve_oauth_issuance(plan=plan, slot=slot,
                                                       token_sha256=bearer_sha256(secrets.token_urlsafe(32)),
                                                       record=record, ttl_seconds=3600)

        for slot in ("access", "refresh"):
            for name, wider in changes.items():
                records = _records(w, plan)
                await refused(slot, {**records[slot], name: wider})  # the record itself
                envelope = dict(records[slot]["credential"])
                envelope["attrs"] = {**envelope["attrs"], name: wider}
                await refused(slot, {**records[slot], "credential": envelope})  # its envelope
                missing = {key: value for key, value in records[slot].items() if key != name}
                await refused(slot, missing)
                await refused(slot, {**records[slot], name: "memories.write"})  # malformed, not split
            # The SDK's own concrete-URL map (what _issue_tokens builds today) is not the Card's.
            concrete = {RESOURCE: list(plan.resource_grants[declared])}
            await refused(slot, {**_records(w, plan)[slot], "resource_grants": concrete})
        assert await w.authority.issuance_reservations(plan.transaction_id) == {}
        # The exact snapshot is accepted, and the plan is unchanged on replay.
        await _reserve(w, plan)
        assert await _begin(w) == plan



@pytest.mark.asyncio
async def test_more_concurrent_completions_than_pool_connections_never_hang(tmp_path):
    """Different Cards, n above the shared pool's max_size (6): every completion answers within a bound."""
    import asyncio

    async with _world(tmp_path) as w:
        plans = []
        for index in range(9):
            plan = await _begin(w, request=f"exchange-pool-{index}", grantor_subject=f"user-w603-pool-{index}")
            tokens = await _reserve(w, plan)
            plans.append((plan, tokens))
        results = await asyncio.wait_for(asyncio.gather(*(
            w.service.complete_oauth_issuance(transaction_id=plan.transaction_id) for plan, _ in plans)), timeout=60)
        assert {result.state for result in results} <= {"committed", "pending"}, [r.state for r in results]
        for plan, tokens in plans:
            final = await asyncio.wait_for(w.service.complete_oauth_issuance(transaction_id=plan.transaction_id),
                                           timeout=30)
            assert final.state == "committed", final
            assert await _usable(w, tokens) == {"access": True, "refresh": True}
        assert await w.decisions.list_in_doubt(limit=20) == [] and await tx.list_in_doubt(w.store) == []


@pytest.mark.asyncio
async def test_a_completion_whose_claim_lapsed_to_another_decides_nothing(tmp_path):
    """Another completion (another machine) took the claim while this one prepared: this one must not commit."""
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        tokens = await _reserve(w, plan)
        coordinator = w.service._card_coordinator[0]
        real_prepare = coordinator.prepare_existing

        async def prepare_then_lose_the_claim(transaction_id):
            record = await real_prepare(transaction_id)
            async with w.pool.acquire() as connection:  # the claim lapsed and another holder took it
                await connection.execute(
                    f"UPDATE {w.authority.schema}.connection_hub_oauth_issuance_plans "
                    f"SET completing_owner = 'another-machine', completing_until = clock_timestamp() + interval '60 s' "
                    f"WHERE transaction_id = $1", transaction_id)
            return record

        coordinator.prepare_existing = prepare_then_lose_the_claim
        first = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        coordinator.prepare_existing = real_prepare
        assert first.state == "pending" and (await w.decisions.read(plan.transaction_id)).state not in (
            "committed", "aborted")
        assert await _usable(w, tokens) == {"access": False, "refresh": False}
        # The other holder's claim still stands: a retry answers pending, never decides over it.
        assert (await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)).state == "pending"
        async with w.pool.acquire() as connection:  # that holder ends (or its claim lapses)
            await connection.execute(
                f"UPDATE {w.authority.schema}.connection_hub_oauth_issuance_plans "
                f"SET completing_owner = '', completing_until = NULL WHERE transaction_id = $1", plan.transaction_id)
        final = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert final.state == "committed" and await _usable(w, tokens) == {"access": True, "refresh": True}



@pytest.mark.asyncio
async def test_a_reconsent_on_postgres_handles_moves_the_row_once_and_stays_readable(tmp_path):
    """W606 one writer: a create's credential_issue writes the row; a re-consent's handle_binding only moves it."""
    async with _world(tmp_path, postgres_handles=True) as w:
        first = await _begin(w)
        await _reserve(w, first)
        await w.service.complete_oauth_issuance(transaction_id=first.transaction_id)
        created = await w.metadata.read_current(first.access_id)
        assert (created.card_revision, created.revision) == (1, 1)  # written once, by the create
        plan = await _begin(w, request="exchange-2", scopes=["memories:read", "memories:write"],
                            resource_grants={RESOURCE: ["memories:read", "memories:write"]})
        assert [key for key in plan.effect_digests if key.startswith("handle:")] == [f"handle:{plan.access_id}"]
        await _reserve(w, plan)
        handles = w.service._cards()._handles
        real_write, writes = handles.write, []

        async def counted_write(*args, **kwargs):
            writes.append(args[0].card_revision)
            return await real_write(*args, **kwargs)

        handles.write = counted_write  # the issuance target's metadata write
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        handles.write = real_write
        assert result.state == "committed"
        assert writes == []  # a re-consent never rewrites the row through credential_issue
        moved = await w.metadata.read_current(plan.access_id)
        assert (moved.card_revision, moved.expires_at, moved.revision) == (2, plan.expires_at, 2)  # one more write
        assert moved.session_id == created.session_id
        # The strict reader accepts the committed Card with its moved row (this world's serving
        # projection is fake, so the check reads the committed Card directly).
        committed = (await w.store.read_current_authority(subject_hash=w.subject_hash,
                                                          access_id=plan.access_id))[1]
        held = await w.service._cards()._handles.read(committed)
        assert committed.card_revision == 2 and held.access_id == plan.access_id


@pytest.mark.asyncio
async def test_reading_an_issuance_never_decides_it(tmp_path):
    """W585 recovery reads the original outcome without preparing, deciding or aborting anything."""
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        assert await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id) == plan
        before = await w.service.read_oauth_issuance(transaction_id=plan.transaction_id)
        assert before.state == "pending" and before.receipt_digest == ""
        assert {slot: o.outcome for slot, o in before.per_slot.items()} == {"access": "pending", "refresh": "pending"}
        row = await w.decisions.read(plan.transaction_id)
        assert not row.terminal and row.prepared == {}  # reading prepared and decided nothing
        tokens = await _reserve(w, plan, slots=("access",))  # only one slot so far: a read still decides nothing
        assert (await w.service.read_oauth_issuance(transaction_id=plan.transaction_id)).state == "pending"
        assert not (await w.decisions.read(plan.transaction_id)).terminal
        tokens.update(await _reserve(w, plan, slots=("refresh",)))
        committed = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert await w.service.read_oauth_issuance(transaction_id=plan.transaction_id) == committed
        with pytest.raises(IssuanceRefused, match="issuance_plan_unknown"):
            await w.service.read_oauth_issuance(transaction_id="0" * 64)


@pytest.mark.asyncio
async def test_a_caller_that_lost_begins_answer_reads_the_plan_by_its_decision_request_id(tmp_path):
    """W585: begin committed, the caller died before keeping the transaction id; it reads, never re-begins."""
    from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
    from connection_hub.delegated_credentials.oauth.authority_schema import TABLE_ISSUANCE_PLANS
    from connection_hub.delegated_credentials.oauth_issuance import decision_request_id

    def request_id(original_request_id="exchange-1", client_id=CLIENT):
        # The caller's own copy of the Hub's field-tagged identity (the SDK keeps it before begin).
        return decision_request_id(scope=f"{PARTICIPANT}:oauth-issuance", grantor_subject=GRANTOR,
                                   client_id=client_id, original_request_id=original_request_id)

    async with _world(tmp_path) as w:
        plan = await _begin(w)
        read = w.service.read_oauth_issuance_plan_by_request
        assert plan.decision_request_id == request_id()
        assert await read(decision_request_id=request_id()) == plan
        row = await w.decisions.read(plan.transaction_id)
        assert not row.terminal and row.prepared == {}  # reading prepared and decided nothing
        for other in (request_id("exchange-2"), request_id(client_id="another-client"), "0" * 64):
            with pytest.raises(IssuanceRefused, match="issuance_plan_unknown"):
                await read(decision_request_id=other)
        for invalid in ("exchange-1", request_id().upper(), request_id()[:63], None):
            with pytest.raises(IssuanceRefused, match="issuance_request_invalid"):
                await read(decision_request_id=invalid)
        # A plan stored but never bound to its decision (begin interrupted) is not a plan to complete.
        _c, _i, _d, _ttl, store = w.service._issuance_parts()
        async with store._pool.acquire() as connection:
            await connection.execute(f"UPDATE {store.schema}.{TABLE_ISSUANCE_PLANS} SET transaction_id = NULL "
                                     "WHERE tenant = $1 AND project = $2", store.tenant, store.project)
        with pytest.raises(IssuanceRefused, match="issuance_plan_unbound"):
            await read(decision_request_id=request_id())


@pytest.mark.asyncio
async def test_a_request_row_naming_another_requests_decision_never_returns_that_plan(tmp_path):
    """Main 15:20: the trusted plan must be this request's own; another bound decision answers unknown.

    The table's unique transaction index keeps two rows from naming one
    decision, so the stored request row is substituted at the store read.
    """
    async with _world(tmp_path) as w:
        first, second = await _begin(w, "exchange-1"), await _begin(w, "exchange-2")
        _c, _i, _d, _ttl, store = w.service._issuance_parts()
        original = store.read_issuance_plan_request

        async def crossed(decision_request_id):
            row = await original(decision_request_id)
            if decision_request_id == first.decision_request_id:
                row = {**row, "transaction_id": second.transaction_id}
            return row

        store.read_issuance_plan_request = crossed
        with pytest.raises(IssuanceRefused, match="issuance_plan_unknown"):
            await w.service.read_oauth_issuance_plan_by_request(decision_request_id=first.decision_request_id)
        assert await w.service.read_oauth_issuance_plan_by_request(
            decision_request_id=second.decision_request_id) == second


@pytest.mark.asyncio
async def test_w571_a_revoked_oauth_card_reconsented_continues_its_revision_chain(tmp_path):
    """W571 condition 1, OAuth kind: the id is deterministic, so a re-consent after a revoke reuses it,
    but its revisions only move forward; the id never restarts at revision 1."""
    from dataclasses import replace
    from connection_hub.delegated_credentials.cards.service import CardConflict

    async with _world(tmp_path) as w:
        first = await _begin(w)
        await _reserve(w, first)
        await w.service.complete_oauth_issuance(transaction_id=first.transaction_id)
        issued = await _card(w, first.access_id)
        user = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}
        assert (await w.service.revoke_access(user, access_id=first.access_id)).get("ok"), "revoke"
        revoked = await _card(w, first.access_id)
        assert revoked.state == "revoked" and revoked.card_revision > issued.card_revision
        plan = await _begin(w, request="exchange-after-revoke")
        assert plan.access_id == first.access_id  # the deterministic id is reused...
        assert plan.base_revision == revoked.card_revision and plan.candidate_revision > revoked.card_revision
        await _reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "committed" and result.card_revision > revoked.card_revision  # ...the chain continues
        with pytest.raises(CardConflict):  # and the id can never start over at revision 1
            await w.cards.commit(replace(issued, label="a replaced Card"), subject_hash=w.subject_hash,
                                 expected_revision=0, now=0)

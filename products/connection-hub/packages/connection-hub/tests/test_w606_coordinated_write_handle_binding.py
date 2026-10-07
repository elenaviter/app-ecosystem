"""W606: a coordinated edit of a credential-bearing Card and the PostgreSQL handle binding (DSN-gated).

Real: the Card store (bundle files), the Card service, the Hub participant,
the Coordinator, the PostgreSQL decision store, and the PostgreSQL Card
handle-metadata store behind ``PostgresCardCredentialHandleStore`` with its
strict binding check. Fake: only the serving projection (``_Cache``) and the
resident-secret custody, which a connector Card never reaches (only an agent
Card's bearer is resident). The Card is an owned, synthetic OAuth connector
Card whose opaque handles do not change: the exact case the generic
coordinated writer routes (an update, an extension, a prune, a fold).
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.credential_handles import PostgresCardCredentialHandleStore
from connection_hub.delegated_credentials.cards.handle_authority import PostgresCardHandleMetadataStore
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from test_caller_writer_gate import _card
from test_card_service import _Cache


class _NoResidentCustody:
    """A connector Card never reaches resident custody; any call is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"resident custody reached: {name}")


class _Persistence:
    """The persistence port the service writes through, over the real Card store and handle store."""

    def __init__(self, store, cards, handles):
        self.card_store, self.card_service, self._handles, self._store = store, cards, handles, store
        self.direct = []

    async def load(self, access_id, *, subject_hash):
        current = await self._store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        if current is None:
            return None
        authority = current[1]
        return authority, await self._handles.read(authority)  # the strict binding check, as persistence does

    async def persist(self, authority, handles, *, subject_hash, expected_revision, **_guard):
        self.direct.append(authority.card_revision)
        await self.card_service.commit(authority, subject_hash=subject_hash, expected_revision=expected_revision)
        await self._handles.write(authority, handles)

    async def current_revision(self, access_id, *, subject_hash):
        current = await self._store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        return 0 if current is None else current[1].card_revision


@asynccontextmanager
async def _world(tmp_path):
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg
    from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    namespace = uuid.uuid4().hex[:10]
    schema = f"hub_w606_{namespace}"
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    decisions = PostgresDecisionStore(pool, schema=schema, namespace="connection-hub-w606")
    await decisions.ensure_schema()
    metadata = PostgresCardHandleMetadataStore(pg_pool=pool, tenant=f"t-{namespace}", project="w606")
    await metadata.ensure_schema()
    handles = PostgresCardCredentialHandleStore(metadata_store=metadata, resident_secrets=_NoResidentCustody())

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    now = int(time.time())
    card = dataclasses.replace(_card(bound=False, revision=1), source="oauth", card_kind=CARD_KIND_CONNECTOR,
                               created_at=now - 60, expires_at=now + 3600)
    subject_hash = subject_hash_for(card.grantor_subject)
    await cards.commit(card, subject_hash=subject_hash, expected_revision=0, now=now)
    await handles.write(card, CardCredentialHandles(access_id=card.access_id))  # what the direct path stores
    persistence = _Persistence(store, cards, handles)
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=cards, store=store, intents=intents, decisions=decisions)
    host = object.__new__(AutomationAccessService)
    host._persistence = persistence
    host._caller_writers = None
    host.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, HubLocalReceiptVerifier(store)),
                               intents=intents, decisions=decisions)
    try:
        yield SimpleNamespace(host=host, store=store, cards=cards, handles=handles, metadata=metadata,
                              persistence=persistence, card=card, subject_hash=subject_hash, decisions=decisions)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()


@pytest.mark.asyncio
async def test_reproduce_a_coordinated_edit_leaves_the_handle_binding_at_the_old_revision(tmp_path):
    """The reported case, exactly: unchanged opaque handles, a coordinated edit, then an ordinary load."""
    async with _world(tmp_path) as w:
        loaded = await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash)
        assert loaded is not None and loaded[0] == w.card  # readable before the edit
        edited = dataclasses.replace(w.card, card_revision=w.card.card_revision + 1, label="edited",
                                     expires_at=w.card.expires_at + 600)
        await w.host._persist_record(record_from_card(edited), expected_revision=w.card.card_revision)
        assert w.persistence.direct == []  # it went through the coordinated writer, not the direct path
        committed = (await w.store.read_current_authority(subject_hash=w.subject_hash,
                                                          access_id=w.card.access_id))[1]
        assert committed == edited  # the Card committed revision 2
        bound = await w.metadata.read_current(w.card.access_id)
        print("W606 repro: card", committed.card_revision, committed.expires_at,
              "| metadata", bound.card_revision, bound.expires_at)
        from connection_hub.delegated_credentials.cards.credential_handles import CardCredentialHandleUnavailable
        with pytest.raises(CardCredentialHandleUnavailable, match="card_handle_revision_mismatch"):
            await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash)



@pytest.mark.asyncio
async def test_reproduce_through_the_production_persistence_load(tmp_path):
    """The same edit on one production DurableCardPersistence (PostgreSQL backend, real Redis serving)."""
    if not os.environ.get("REDIS_URL") or not os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN"):
        pytest.skip("REDIS_URL and CONNECTION_HUB_TEST_POSTGRES_DSN are required")
    import asyncpg
    import redis.asyncio as redis_asyncio
    from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

    from connection_hub.delegated_credentials.authority_config import AUTHORITY_BACKEND_POSTGRESQL
    from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
    from connection_hub.delegated_credentials.cards.service import CardServingUnavailable

    pool = await asyncpg.create_pool(os.environ["CONNECTION_HUB_TEST_POSTGRES_DSN"], min_size=1, max_size=6)
    redis_client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    namespace = uuid.uuid4().hex[:10]
    schema = f"hub_w606p_{namespace}"
    metadata = PostgresCardHandleMetadataStore(pg_pool=pool, tenant=f"t-{namespace}", project="w606")
    try:
        async with pool.acquire() as connection:
            await connection.execute(f"CREATE SCHEMA {schema}")
        decisions = PostgresDecisionStore(pool, schema=schema, namespace="connection-hub-w606p")
        await decisions.ensure_schema()
        await metadata.ensure_schema()
        handles = PostgresCardCredentialHandleStore(metadata_store=metadata, resident_secrets=_NoResidentCustody())
        @asynccontextmanager
        async def mutation_lock(**_kwargs):
            yield

        store = BundleStorageDelegatedCardStore(tmp_path)
        tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
        production = DurableCardPersistence(
            redis=redis_client, tenant=f"t-{namespace}", project="w606", card_store=store,
            mutation_lock=mutation_lock, credential_handles=handles, authority_backend=AUTHORITY_BACKEND_POSTGRESQL)
        now = int(time.time())
        card = dataclasses.replace(_card(bound=False, revision=1), source="oauth", card_kind=CARD_KIND_CONNECTOR,
                                   created_at=now - 60, expires_at=now + 3600)
        subject_hash = subject_hash_for(card.grantor_subject)
        await production.persist(card, CardCredentialHandles(access_id=card.access_id), subject_hash=subject_hash,
                                 expected_revision=0)  # the direct path: Card, serving and handles together
        assert (await production.load(card.access_id, subject_hash=subject_hash))[0] == card
        intents = LocalCardIntentSource(store)
        hub = HubCardParticipant(service=production.card_service, store=store, intents=intents, decisions=decisions)
        host = object.__new__(AutomationAccessService)
        host._persistence = production
        host._caller_writers = None
        host.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, HubLocalReceiptVerifier(store)),
                                   intents=intents, decisions=decisions)
        edited = dataclasses.replace(card, card_revision=2, label="edited")
        await host._persist_record(record_from_card(edited), expected_revision=1)
        committed = (await store.read_current_authority(subject_hash=subject_hash, access_id=card.access_id))[1]
        bound = await metadata.read_current(card.access_id)
        print("W606 production repro: card", committed.card_revision, "| metadata", bound.card_revision)
        assert committed == edited and bound.card_revision == 1
        with pytest.raises(CardServingUnavailable, match="credential_handles_unavailable"):
            await production.load(card.access_id, subject_hash=subject_hash)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()
        await redis_client.aclose()

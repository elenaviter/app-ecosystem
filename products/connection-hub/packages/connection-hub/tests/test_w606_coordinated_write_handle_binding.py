"""W606: a coordinated edit of a credential-bearing Card keeps its delivered credential readable (DSN-gated).

Before W606 these tests reproduced the defect: the edit committed a new Card
revision, the handle row stayed at the old one, and the strict reader refused
the credential. They now assert the fix: the edit's ``handle_binding`` effect
moves only the row's serving revision and expiry, so the same credential loads.

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


class _NoGrants:
    """The OAuth grant store, never reached by these edits (no lifetime or unbind effect is planned)."""

    def __getattr__(self, name):
        raise AssertionError(f"grant store reached: {name}")


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
    from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
    compose_card_effects(card_service=cards, card_store=store, grant_store=_NoGrants(), policies=None,
                         credential_handles=handles)
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=cards, store=store, intents=intents, decisions=decisions)
    host = object.__new__(AutomationAccessService)
    host._persistence = persistence
    host._caller_writers = None
    host.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, HubLocalReceiptVerifier(store)),
                               intents=intents, decisions=decisions)
    host.bind_card_credential_handles(handles)
    try:
        yield SimpleNamespace(host=host, store=store, cards=cards, handles=handles, metadata=metadata,
                              persistence=persistence, card=card, subject_hash=subject_hash, decisions=decisions)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()


@pytest.mark.asyncio
async def test_a_coordinated_edit_keeps_the_delivered_credential_readable(tmp_path):
    """The reported case (unchanged opaque handles, a coordinated edit, an ordinary load), now fixed."""
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
        assert (bound.card_revision, bound.expires_at) == (committed.card_revision, committed.expires_at)
        loaded = await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash)
        assert loaded is not None and loaded[0] == edited  # the same credential, readable at the new revision



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
        from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
        compose_card_effects(card_service=production.card_service, card_store=store, grant_store=_NoGrants(),
                             policies=None, credential_handles=handles)
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
        host.bind_card_credential_handles(handles)
        edited = dataclasses.replace(card, card_revision=2, label="edited")
        await host._persist_record(record_from_card(edited), expected_revision=1)
        committed = (await store.read_current_authority(subject_hash=subject_hash, access_id=card.access_id))[1]
        bound = await metadata.read_current(card.access_id)
        assert committed == edited and bound.card_revision == 2
        assert (await production.load(card.access_id, subject_hash=subject_hash))[0] == edited
        del CardServingUnavailable  # the refusal this test once reproduced no longer occurs
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()
        await redis_client.aclose()


def _row_identity(row):
    """What a remote party's credential depends on: everything but the serving revision and expiry."""
    return (row.access_id, row.resident_access_secret_ref, row.resident_access_sha256, row.session_id, row.state)


async def _edit(w, **changes):
    edited = dataclasses.replace(w.card, card_revision=w.card.card_revision + 1, **changes)
    await w.host._persist_record(record_from_card(edited), expected_revision=w.card.card_revision)
    return edited


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["label", "extend", "shorten", "prune"])
async def test_every_edit_kind_moves_only_the_serving_binding(tmp_path, change):
    async with _world(tmp_path) as w:
        before = await w.metadata.read_current(w.card.access_id)
        changes = {"label": {"label": "edited"}, "extend": {"expires_at": w.card.expires_at + 600},
                   "shorten": {"expires_at": w.card.expires_at - 600},
                   "prune": {"account_scope": {}, "resource_grants": {}}}[change]
        edited = await _edit(w, **changes)
        after = await w.metadata.read_current(w.card.access_id)
        assert (after.card_revision, after.expires_at) == (edited.card_revision, edited.expires_at)
        assert _row_identity(after) == _row_identity(before)  # the held credential is byte-identical
        assert (await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash))[0] == edited


@pytest.mark.asyncio
async def test_an_aborted_edit_leaves_the_binding_and_the_card_unchanged(tmp_path):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused

    async with _world(tmp_path) as w:
        before = await w.metadata.read_current(w.card.access_id)
        edited = dataclasses.replace(w.card, card_revision=2, label="edited")

        async def refusing_gate():
            raise CallerWriteRefused("pb_refused")

        with pytest.raises(CallerWriteRefused):
            await w.host._coordinated_write(record_from_card(edited), edited, expected_revision=1, caller_write=None,
                                            gate=refusing_gate, witness="")
        assert await w.metadata.read_current(w.card.access_id) == before
        assert (await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash))[0] == w.card


@pytest.mark.asyncio
async def test_a_row_that_moved_before_stage_aborts_the_edit(tmp_path):
    """The identity pinned in the intent no longer matches at STAGE: the edit ABORTs, the Card stays readable."""
    from connection_hub.delegated_credentials.cards.handle_metadata import CardHandleMetadata
    from connection_hub.delegated_credentials.cards.service import CardConflict

    async with _world(tmp_path) as w:
        coordinator = w.host._card_coordinator[0]
        real_prepare = coordinator.prepare_existing

        async def move_the_row_then_prepare(transaction_id):
            current = await w.metadata.read_current(w.card.access_id)
            await w.metadata.put(CardHandleMetadata(access_id=current.access_id, card_revision=current.card_revision,
                                                    expires_at=current.expires_at, session_id="another-session"),
                                 expected_revision=current.revision)
            return await real_prepare(transaction_id)

        coordinator.prepare_existing = move_the_row_then_prepare
        with pytest.raises(CardConflict):
            await _edit(w, label="edited")
        assert (await w.store.read_current_authority(subject_hash=w.subject_hash,
                                                     access_id=w.card.access_id))[1] == w.card
        row = await w.metadata.read_current(w.card.access_id)
        assert (row.card_revision, row.session_id) == (1, "another-session")  # untouched by the edit


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["replaced_row", "revoked_row", "newer_card"])
async def test_a_stale_application_is_superseded_and_changes_nothing(tmp_path, stale):
    """COMMIT recorded, application interrupted; then the row or the Card moves: the late apply writes nothing."""
    from datetime import datetime, timezone

    from connection_hub.delegated_credentials.cards.handle_metadata import CardHandleMetadata
    from connection_hub.delegated_credentials.durable_io import write_json_atomic

    async with _world(tmp_path) as w:
        real_advance = w.handles.advance_binding

        async def lost(*_args, **_kwargs):
            raise ConnectionError("database connection lost")

        w.handles.advance_binding = lost
        from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
        with pytest.raises(CardServingUnavailable):
            await _edit(w, label="edited")  # committed; its effect is pending
        current = await w.metadata.read_current(w.card.access_id)
        if stale == "replaced_row":
            await w.metadata.put(CardHandleMetadata(access_id=current.access_id, card_revision=current.card_revision,
                                                    expires_at=current.expires_at, session_id="re-issued"),
                                 expected_revision=current.revision)
        elif stale == "revoked_row":
            await w.metadata.retire(w.card.access_id, expected_revision=current.revision, state="revoked")
        else:
            newer = dataclasses.replace(w.card, card_revision=3, label="newer")
            pointer = await w.store.write_revision(subject_hash=w.subject_hash, authority=newer,
                                                   updated_at=datetime.now(timezone.utc))
            await write_json_atomic(w.store.current_path(subject_hash=w.subject_hash, access_id=w.card.access_id),
                                    pointer.to_dict())  # forced past the store's fence, as an out-of-band writer
        before = await w.metadata.read_current(w.card.access_id)
        w.handles.advance_binding = real_advance
        await w.host._card_coordinator[0].recover(limit=10)
        assert await w.metadata.read_current(w.card.access_id) == before  # nothing written, nothing revived


@pytest.mark.asyncio
async def test_an_interrupted_application_recovers_and_replays_idempotently(tmp_path):
    async with _world(tmp_path) as w:
        real_advance = w.handles.advance_binding
        calls = {"left": 1}

        async def lose_once(*args, **kwargs):
            if calls["left"]:
                calls["left"] -= 1
                raise ConnectionError("database connection lost")
            return await real_advance(*args, **kwargs)

        w.handles.advance_binding = lose_once
        from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
        with pytest.raises(CardServingUnavailable):
            await _edit(w, label="edited")
        await w.host._card_coordinator[0].recover(limit=10)
        moved = await w.metadata.read_current(w.card.access_id)
        assert moved.card_revision == 2
        await w.host._card_coordinator[0].recover(limit=10)  # a second pass changes nothing
        assert await w.metadata.read_current(w.card.access_id) == moved
        assert (await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash))[0].card_revision == 2

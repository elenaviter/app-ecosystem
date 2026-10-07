"""W606 cut 2: a coordinated edit of a hosted-agent Card keeps serving the SAME resident bearer (DSN-gated).

A resident secret's envelope is bound to the Card revision and expiry, so the
row moves by re-wrapping the same bearer under a fresh ref through the
existing prepared-secret path. Real: the Card store, Card service, Hub
participant, Coordinator, PostgreSQL decision store, PostgreSQL handle-metadata
store and the ResidentCardSecretService. Synthetic: the secret store behind it
(the in-memory double of the existing resident-secret tests) and the bearer.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
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
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.handle_authority import PostgresCardHandleMetadataStore
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from connection_hub.delegated_credentials.cards.resident_secrets.service import ResidentCardSecretService
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable, DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from test_caller_writer_gate import _card
from test_card_service import _Cache
from test_resident_card_secrets import _SecretStore
from test_w606_coordinated_write_handle_binding import _NoGrants, _Persistence


@asynccontextmanager
async def _world(tmp_path):
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg
    from service_foundation.coordination.durable_decision_log import Coordinator, PostgresDecisionStore

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    namespace = uuid.uuid4().hex[:10]
    schema = f"hub_w606a_{namespace}"
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    decisions = PostgresDecisionStore(pool, schema=schema, namespace="connection-hub-w606a")
    await decisions.ensure_schema()
    metadata = PostgresCardHandleMetadataStore(pg_pool=pool, tenant=f"t-{namespace}", project="w606a")
    await metadata.ensure_schema()
    secret_store = _SecretStore()
    resident = ResidentCardSecretService(metadata_store=metadata, secret_store=secret_store)
    handles = PostgresCardCredentialHandleStore(metadata_store=metadata, resident_secrets=resident)

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    now = int(time.time())
    card = dataclasses.replace(_card(bound=False, revision=1), source="manual", card_kind=CARD_KIND_AGENT,
                               created_at=now - 60, expires_at=now + 3600)
    subject_hash = subject_hash_for(card.grantor_subject)
    bearer = "kst1." + secrets.token_urlsafe(32)
    await cards.commit(card, subject_hash=subject_hash, expected_revision=0, now=now)
    await handles.write(card, CardCredentialHandles(access_id=card.access_id, access_token=bearer,
                                                    session_id="session-1"))
    persistence = _Persistence(store, cards, handles)
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
        yield SimpleNamespace(host=host, store=store, handles=handles, metadata=metadata, resident=resident,
                              secret_store=secret_store, persistence=persistence, card=card, bearer=bearer,
                              subject_hash=subject_hash)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await connection.execute(f"DROP SCHEMA IF EXISTS {metadata.schema} CASCADE")
        await pool.close()


async def _edit(w, **changes):
    edited = dataclasses.replace(w.card, card_revision=w.card.card_revision + 1, **changes)
    # As every caller does: the record it edits was loaded with its handles, which stay unchanged.
    held = CardCredentialHandles(access_id=w.card.access_id, access_token=w.bearer, session_id="session-1")
    await w.host._persist_record(record_from_card(edited, held), expected_revision=w.card.card_revision)
    assert w.persistence.direct == []  # through the coordinated writer, never the direct path
    return edited


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["label", "extend", "shorten"])
async def test_an_agent_card_edit_keeps_serving_the_same_resident_bearer(tmp_path, change):
    async with _world(tmp_path) as w:
        before = await w.metadata.read_current(w.card.access_id)
        changes = {"label": {"label": "edited"}, "extend": {"expires_at": w.card.expires_at + 600},
                   "shorten": {"expires_at": w.card.expires_at - 600}}[change]
        edited = await _edit(w, **changes)
        after = await w.metadata.read_current(w.card.access_id)
        assert (after.card_revision, after.expires_at) == (edited.card_revision, edited.expires_at)
        assert after.resident_access_secret_ref != before.resident_access_secret_ref  # a fresh custody object
        assert (after.resident_access_sha256, after.session_id) == (before.resident_access_sha256, "session-1")
        held = (await w.persistence.load(w.card.access_id, subject_hash=w.subject_hash))[1]
        assert held.access_token == w.bearer  # the agent still holds and is served the same bearer
        assert await w.resident.resolve(w.card.access_id) == w.bearer  # the envelope is at the AFTER Card


@pytest.mark.asyncio
async def test_an_interrupted_rewrap_recovers_with_the_same_bearer(tmp_path):
    """The install's outcome is lost after its secret was written: recovery re-wraps once and serves the same bearer."""
    async with _world(tmp_path) as w:
        real_install = w.metadata.install_prepared_resident_secret
        calls = {"left": 1}

        async def lose_the_commit_once(*args, **kwargs):
            if calls["left"]:
                calls["left"] -= 1
                raise ConnectionError("database connection lost")
            return await real_install(*args, **kwargs)

        w.metadata.install_prepared_resident_secret = lose_the_commit_once
        with pytest.raises(CardServingUnavailable):
            await _edit(w, label="edited")
        assert (await w.metadata.read_current(w.card.access_id)).card_revision == 1  # the row has not moved
        await w.host._card_coordinator[0].recover(limit=10)
        moved = await w.metadata.read_current(w.card.access_id)
        assert moved.card_revision == 2
        assert await w.resident.resolve(w.card.access_id) == w.bearer
        await w.host._card_coordinator[0].recover(limit=10)  # a replay changes nothing
        assert await w.metadata.read_current(w.card.access_id) == moved


@pytest.mark.asyncio
async def test_a_reissued_agent_secret_before_apply_is_superseded(tmp_path):
    """The agent's secret was re-issued after COMMIT and before apply: the late re-wrap writes nothing."""
    async with _world(tmp_path) as w:
        real_commit = w.handles.commit_rewrap

        async def lost(*_args, **_kwargs):
            raise ConnectionError("database connection lost")

        w.handles.commit_rewrap = lost
        with pytest.raises(CardServingUnavailable):
            await _edit(w, label="edited")
        current = await w.metadata.read_current(w.card.access_id)
        reissued = "kst1." + secrets.token_urlsafe(32)
        await w.resident.install(access_id=w.card.access_id, card_revision=current.card_revision, bearer=reissued,
                                 session_id="session-2", expires_at=current.expires_at,
                                 expected_revision=current.revision)
        before = await w.metadata.read_current(w.card.access_id)
        w.handles.commit_rewrap = real_commit
        await w.host._card_coordinator[0].recover(limit=10)
        assert await w.metadata.read_current(w.card.access_id) == before
        assert await w.resident.resolve(w.card.access_id) == reissued  # never overwritten with the old bearer
        # The envelope STAGE prepared for the superseded edit is discarded, not left serving anything.
        prepared_ref = w.resident.rewrap_secret_ref(_lead_transaction(w), w.card.access_id)
        assert prepared_ref not in w.secret_store.values



def _lead_transaction(w) -> str:
    """The transaction id the edit's effect was bound under (its single-Card receipt)."""
    import json
    import pathlib

    receipts = sorted(pathlib.Path(w.store.root / "card-transactions").glob("*.json"))
    return json.loads(receipts[-1].read_text())["transaction_id"]


@pytest.mark.asyncio
async def test_a_rewrap_that_cannot_succeed_refuses_at_stage_and_nothing_commits(tmp_path):
    """The bearer no longer resolves: STAGE refuses, the edit ABORTs, the Card stays at its revision."""
    from connection_hub.delegated_credentials.cards.service import CardConflict

    async with _world(tmp_path) as w:
        current = await w.metadata.read_current(w.card.access_id)
        from connection_hub.delegated_credentials.cards.resident_secrets.model import ResidentSecretError

        async def cannot_prepare(_prepared):
            raise ResidentSecretError("resident_secret_store_unavailable", access_id=w.card.access_id)

        w.resident.prepare_rewrap = cannot_prepare  # the custody write the re-wrap needs fails at STAGE
        with pytest.raises(CardConflict):
            await _edit(w, label="edited")
        committed = (await w.store.read_current_authority(subject_hash=w.subject_hash,
                                                          access_id=w.card.access_id))[1]
        assert committed == w.card  # never committed into a Card whose bearer would stop serving
        assert await w.metadata.read_current(w.card.access_id) == current


@pytest.mark.asyncio
async def test_an_aborted_edit_discards_the_prepared_envelope_and_keeps_the_old_one(tmp_path):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused

    async with _world(tmp_path) as w:
        before = await w.metadata.read_current(w.card.access_id)
        edited = dataclasses.replace(w.card, card_revision=2, label="edited")
        held = CardCredentialHandles(access_id=w.card.access_id, access_token=w.bearer, session_id="session-1")

        async def refusing_gate():
            raise CallerWriteRefused("pb_refused")

        with pytest.raises(CallerWriteRefused):
            await w.host._coordinated_write(record_from_card(edited, held), edited, expected_revision=1,
                                            caller_write=None, gate=refusing_gate, witness="")
        assert await w.metadata.read_current(w.card.access_id) == before
        assert await w.resident.resolve(w.card.access_id) == w.bearer
        prepared_ref = w.resident.rewrap_secret_ref(_lead_transaction(w), w.card.access_id)
        assert prepared_ref not in w.secret_store.values  # STAGE's envelope was discarded by the ABORT

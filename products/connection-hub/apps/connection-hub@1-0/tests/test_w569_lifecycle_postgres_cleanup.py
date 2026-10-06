"""W569: pair-revoke credential cleanup against real PostgreSQL handle custody.

The hosted PostgreSQL authority stores each credentialed Card's handle
metadata in PostgreSQL (``postgres_card_credential_handle_store``). The pair
revoke removes both handles after its durable commit, while both Card fences
are held. Cleanup failing part-way must leave committed/serving-pending (never a
no-write refusal), keep both Cards closed, and finish on the identical retry.
Repeating the retry must not repeat cleanup.

Real parts: PostgreSQL (``CONNECTION_HUB_TEST_POSTGRES_DSN``), Redis
(``REDIS_URL``), BundleStorage, and the KDCube ``DurableCardPersistence`` with
its real fences. The Cards are session-only automation Cards, whose
custody is PostgreSQL metadata alone. The resident secret store is never
expected: it fails the test if called. The one injected fault is the second
handle removal raising once, at the store boundary.
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from connection_hub.delegated_credentials.authority_config import AUTHORITY_BACKEND_POSTGRESQL
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.model import CardAuthority, CardCredentialHandles, NamedServiceSelection
from connection_hub.delegated_credentials.cards.service import CardConflict
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.credential_handles import (
    postgres_card_credential_handle_store,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from test_w569_lifecycle_real_redis import ACTOR, _durable, _request, _seed

pytestmark = pytest.mark.skipif(
    not (os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN") and os.environ.get("REDIS_URL")),
    reason="CONNECTION_HUB_TEST_POSTGRES_DSN and REDIS_URL are required for real-PostgreSQL cleanup",
)


class _NoResidentSecrets:
    """Session-only Cards keep no resident secret: any call is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"resident secret store used: {name}")


@pytest_asyncio.fixture
async def backends():
    import asyncpg
    import redis.asyncio as redis_asyncio

    pool = await asyncpg.create_pool(os.environ["CONNECTION_HUB_TEST_POSTGRES_DSN"], min_size=1, max_size=4)
    client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    yield pool, client
    await client.aclose()
    await pool.close()


def _credentialed_pair():
    expires = int(time.time()) + 3600
    return tuple(CardAuthority(
        access_id=f"auto-{index}", grantor_subject=f"owner-{index}", source="manual",
        client_id=f"client-{index}", delegate_subject=f"delegate-{index}",
        card_kind=CARD_KIND_AUTOMATION, card_revision=1, state="active", expires_at=expires,
        issuer_kind=f"opaque-{index}", issuer_ref="opaque-lineage", resource_grants={}, resource_operations={},
        named_service_operations=NamedServiceSelection.none(),
    ) for index in range(2))


async def _decision(_authorities):
    return datetime.now(timezone.utc) + timedelta(seconds=30)


async def _handle_states(handles, cards):
    states = []
    for card in cards:
        current = await handles._metadata.read_current(card.access_id)
        states.append(None if current is None else current.state)
    return states


@pytest.mark.asyncio
async def test_partial_postgres_cleanup_stays_committed_pending_closed_and_finishes_once_on_retry(tmp_path, backends):
    pool, client = backends
    tenant, project = f"t-{uuid.uuid4().hex[:8]}", f"p-{uuid.uuid4().hex[:8]}"
    handles = postgres_card_credential_handle_store(pg_pool=pool, tenant=tenant, project=project,
                                                    secret_store=_NoResidentSecrets())
    await handles.ensure_schema()
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    persistence = DurableCardPersistence(redis=client, tenant=tenant, project=project, card_store=store,
                                         credential_handles=handles, authority_backend=AUTHORITY_BACKEND_POSTGRESQL)
    cards = _credentialed_pair()
    await _seed(store, cards)
    for card in cards:
        await handles.write(card, CardCredentialHandles(access_id=card.access_id, session_id=f"session-{card.access_id}"))
    assert await _handle_states(handles, cards) == ["active", "active"]

    removals = []
    original_remove = handles.remove

    async def remove_once_failing(authority):
        removals.append(authority.access_id)
        if len(removals) == 2:
            raise OSError("synthetic PostgreSQL outage during the second handle removal")
        await original_remove(authority)

    handles.remove = remove_once_failing
    request = _request(cards)

    pending = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=_decision)

    # Committed authority, cleanup incomplete: never a no-write refusal.
    assert (pending["state"], pending["serving_state"]) == ("committed", "pending"), pending
    assert await _durable(store, cards) == [("revoked", 2), ("revoked", 2)]
    assert await _handle_states(handles, cards) == ["revoked", "active"]
    for card in cards:
        with pytest.raises(CardConflict, match="lifecycle_preparation_unresolved"):
            await persistence._cards.commit(dataclasses.replace(card, card_revision=3),
                                            subject_hash=subject_hash_for(card.grantor_subject), expected_revision=2)

    completed = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=_decision)

    assert (completed["state"], completed["serving_state"]) == ("committed", "complete"), completed
    assert await _handle_states(handles, cards) == ["revoked", "revoked"]
    removed_before_replay = list(removals)

    replayed = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=_decision)

    assert replayed == completed
    assert removals == removed_before_replay, "a completed receipt replay repeats no cleanup"
    assert await _handle_states(handles, cards) == ["revoked", "revoked"]

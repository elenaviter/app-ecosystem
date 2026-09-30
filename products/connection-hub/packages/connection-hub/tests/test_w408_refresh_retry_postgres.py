"""W408: a refresh whose response was lost may be retried once, and nothing else is.

The retry is bound to the request it retries (attempt id, client, resource,
scope), to the presented generation as the parent of an unused successor, to a
live family and to a window. Everything else keeps today's rule: presenting a
consumed token revokes the family. Identifiers are synthetic.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.oauth.authority_store import (
    MAX_REFRESH_RETRIES,
    REFRESH_RETRY_WINDOW_SECONDS,
    PostgresOAuthAuthorityStore,
    refresh_request_fingerprint,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStore,
    RefreshTokenReuseDetected,
)

CLIENT = "client-w408"
ATTEMPT = "attempt-" + "a" * 40
OTHER_ATTEMPT = "attempt-" + "b" * 40


def _fingerprint(attempt=ATTEMPT, *, client=CLIENT, resource="", scope=""):
    return refresh_request_fingerprint(refresh_attempt=attempt, client_id=client, resource=resource, scope=scope)


@asynccontextmanager
async def _authority():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    authority = PostgresOAuthAuthorityStore(
        pg_pool=pool, tenant=f"w408-{uuid.uuid4().hex}", project="refresh-retry",
    )
    store = GrantStore(object(), tenant=authority.tenant, project=authority.project, authority_store=authority)
    try:
        await authority.ensure_schema()
        held = await store.create_refresh_token(
            client_id=CLIENT, sub="user-w408", scopes=["records:read"],
            registry_access_id="aut_w408", card_kind="automation",
        )
        yield pool, authority, store, held
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
        await pool.close()


async def _refresh(store: GrantStore, token: str, fingerprint: str = ""):
    """The token route's two store calls, with or without a retry fingerprint."""

    state = await store.get_refresh_token_state(token, refresh_request_fingerprint=fingerprint)
    if state is None:
        return None
    return await store.rotate_refresh_token(token, state=state, refresh_request_fingerprint=fingerprint)


async def _family(pool, authority) -> str:
    async with pool.acquire() as connection:
        return await connection.fetchval(
            f"SELECT state FROM {authority.schema}.connection_hub_oauth_credential_families LIMIT 1"
        )


async def _lost(store, held):
    """One refresh the server committed and whose response the client never got."""

    lost_successor = await _refresh(store, held, _fingerprint())
    assert lost_successor
    return lost_successor


@pytest.mark.asyncio
async def test_a_lost_response_is_retried_once_and_the_family_survives():
    async with _authority() as (pool, authority, store, held):
        lost_successor = await _lost(store, held)
        retried = await _refresh(store, held, _fingerprint())
        assert retried and retried != lost_successor
        assert await _family(pool, authority) == "active"
        # The successor whose response was lost can never be used.
        assert await store.get_refresh_token_state(lost_successor) is None
        # The retried token renews normally afterwards.
        assert await _refresh(store, retried, _fingerprint(OTHER_ATTEMPT))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        _fingerprint(OTHER_ATTEMPT),
        _fingerprint(client="another-client"),
        _fingerprint(resource="https://other.example.test/"),
        _fingerprint(scope="records:write"),
    ],
    ids=["no-attempt", "other-attempt", "other-client", "other-resource", "other-scope"],
)
async def test_a_retry_that_does_not_match_the_lost_request_is_reuse(fingerprint):
    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, fingerprint)
        assert await _family(pool, authority) == "revoked"


@pytest.mark.asyncio
async def test_a_retry_after_the_successor_was_used_is_reuse():
    async with _authority() as (pool, authority, store, held):
        successor = await _lost(store, held)
        # The response was not lost after all: the successor was used.
        assert await _refresh(store, successor, _fingerprint(OTHER_ATTEMPT))
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, _fingerprint())
        assert await _family(pool, authority) == "revoked"


@pytest.mark.asyncio
async def test_repeated_losses_of_one_attempt_are_retried_up_to_the_cap():
    """W408 review: a second post-send loss must not end the family."""

    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        for _ in range(MAX_REFRESH_RETRIES):
            # Each retry's response is lost too; the family stays alive.
            assert await _refresh(store, held, _fingerprint())
            assert await _family(pool, authority) == "active"
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, _fingerprint())
        assert await _family(pool, authority) == "revoked"


@pytest.mark.asyncio
async def test_the_last_retry_that_arrives_is_the_live_token():
    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        first = await _refresh(store, held, _fingerprint())
        second = await _refresh(store, held, _fingerprint())
        assert await store.get_refresh_token_state(first) is None
        assert await _refresh(store, second, _fingerprint(OTHER_ATTEMPT))
        assert await _family(pool, authority) == "active"


async def _consumed_seconds_ago(pool, authority, seconds: int) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            f"UPDATE {authority.schema}.connection_hub_oauth_refresh_generations "
            f"SET consumed_at = now() - ({seconds} * interval '1 second') "
            "WHERE state = 'consumed'"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [900, 1140])
async def test_the_fourth_backoff_retry_is_inside_the_window(seconds):
    """The relay's fourth channel retry comes 900 s after the failure, up to 1140 s with cycle delay."""

    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        await _consumed_seconds_ago(pool, authority, seconds)
        assert await _refresh(store, held, _fingerprint())
        assert await _family(pool, authority) == "active"


@pytest.mark.asyncio
async def test_a_retry_after_the_window_is_reuse():
    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        async with pool.acquire() as connection:
            await connection.execute(
                f"UPDATE {authority.schema}.connection_hub_oauth_refresh_generations "
                f"SET consumed_at = now() - ({REFRESH_RETRY_WINDOW_SECONDS + 1} * interval '1 second') "
                "WHERE state = 'consumed'"
            )
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, _fingerprint())
        assert await _family(pool, authority) == "revoked"


@pytest.mark.asyncio
async def test_a_revoked_family_has_no_retry():
    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, "")
        # Once revoked, even the matching retry is only unknown.
        assert await _refresh(store, held, _fingerprint()) is None


@pytest.mark.asyncio
async def test_concurrent_retries_of_one_attempt_keep_the_family_and_one_live_token():
    """Duplicated delivery of one retry: both are retries, serialised by the row locks."""

    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        results = await asyncio.gather(
            _refresh(store, held, _fingerprint()),
            _refresh(store, held, _fingerprint()),
        )
        assert all(isinstance(r, str) and r for r in results)
        assert await _family(pool, authority) == "active"
        live = [r for r in results if await store.get_refresh_token_state(r) is not None]
        assert len(live) == 1


@pytest.mark.asyncio
async def test_w291_rollback_of_a_retried_rotation_restores_the_presented_generation():
    async with _authority() as (pool, authority, store, held):
        await _lost(store, held)
        state = await store.get_refresh_token_state(held, refresh_request_fingerprint=_fingerprint())
        assert state is not None and state.retry_of
        withheld = await store.rotate_refresh_token(held, state=state, refresh_request_fingerprint=_fingerprint())
        assert withheld
        assert await store.rollback_refresh_token_rotation(held, withheld, state=state) is True
        assert await store.get_refresh_token_state(withheld) is None
        # The presented generation is live again and renews normally.
        assert await _refresh(store, held, "")
        assert await _family(pool, authority) == "active"


@pytest.mark.asyncio
async def test_a_legacy_refresh_without_an_attempt_is_unchanged():
    async with _authority() as (pool, authority, store, held):
        renewed = await _refresh(store, held, "")
        assert renewed
        with pytest.raises(RefreshTokenReuseDetected):
            await _refresh(store, held, "")
        assert await _family(pool, authority) == "revoked"

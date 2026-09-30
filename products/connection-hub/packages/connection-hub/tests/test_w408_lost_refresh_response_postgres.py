"""W408: a lost token response turns the next refresh into reuse and revokes the family.

The live case (2026-09-30, host dev-main): a relay's refresh POST reached the
token endpoint, the server committed the rotation, and the client's read of
the response failed (``ReadError``). The client kept its old refresh token, as
it must when no response arrived. Its next refresh presented that consumed
token, reuse detection revoked the whole credential family, and every later
renewal was refused as unknown.

This reproduces that sequence with the real OAuth client and the real
PostgreSQL authority store. The only fake is the transport, which runs the
server's refresh step and then loses the response. Identifiers are synthetic.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import Mapping
from typing import Any

import pytest

from connection_hub.caller.authorization.client import OAuthClient
from connection_hub.caller.authorization.discovery import _request_error
from connection_hub.caller.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStore,
    RefreshTokenReuseDetected,
)

TOKEN_ENDPOINT = "https://hub.example.test/oauth/token"


class _LostResponseTransport:
    """The token endpoint's refresh step, with one scripted transport failure.

    ``fail_next`` names what happens to the next refresh:

    - ``"read_error"``: the server commits, then the client's read fails (the
      live ReadError, an unknown post-send outcome).
    - ``"connect_error"``: the request never leaves the client (connect
      failure before send). The server does nothing.
    - ``"cancel_before_send"`` and ``"cancel_after_commit"``: the caller's
      task is cancelled before the request is sent, or after the server
      committed and before the response is read.
    """

    def __init__(self, store: GrantStore) -> None:
        self.store = store
        self.fail_next = ""
        self.server_outcomes: list[str] = []

    async def post_form(self, url: str, payload: Mapping[str, str]) -> Mapping[str, Any]:
        failure, self.fail_next = self.fail_next, ""
        if failure == "connect_error":
            raise _request_error(
                failure_code="oauth_token_request_failed", method="POST",
                endpoint=url, failure_kind="ConnectError",
            )
        if failure == "cancel_before_send":
            raise asyncio.CancelledError()
        presented = payload["refresh_token"]
        try:
            state = await self.store.get_refresh_token_state(presented)
        except RefreshTokenReuseDetected:
            self.server_outcomes.append("reuse_detected_family_revoked")
            raise _request_error(
                failure_code="oauth_token_request_failed", method="POST",
                endpoint=url, status=400, server_reason="invalid_grant", oauth_error="invalid_grant",
            )
        if state is None:
            self.server_outcomes.append("refresh_token_unknown")
            raise _request_error(
                failure_code="oauth_token_request_failed", method="POST",
                endpoint=url, status=400, server_reason="invalid_grant", oauth_error="invalid_grant",
            )
        rotated = await self.store.rotate_refresh_token(presented, state=state)
        self.server_outcomes.append("rotated")
        if failure == "read_error":
            # The server committed; the client's read fails (the live ReadError).
            raise _request_error(
                failure_code="oauth_token_request_failed", method="POST",
                endpoint=url, failure_kind="ReadError",
            )
        if failure == "cancel_after_commit":
            raise asyncio.CancelledError()
        return {
            "access_token": f"access-{uuid.uuid4().hex}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": rotated,
        }

    async def get_json(self, url: str) -> Mapping[str, Any]:  # pragma: no cover - unused
        raise AssertionError("no discovery in this reproduction")

    async def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
        raise AssertionError("no registration in this reproduction")


def _metadata() -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata(
        issuer="https://hub.example.test",
        authorization_endpoint="https://hub.example.test/oauth/authorize",
        token_endpoint=TOKEN_ENDPOINT,
        registration_endpoint=None,
        revocation_endpoint=None,
        scopes_supported=(),
        supports_refresh=True,
        authorization_response_issuer_required=False,
    )


def _registration() -> OAuthClientRegistration:
    return OAuthClientRegistration(client_id="client-w408", redirect_uris=("http://127.0.0.1/callback",))


@asynccontextmanager
async def _authority():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    authority = PostgresOAuthAuthorityStore(
        pg_pool=pool, tenant=f"w408-{uuid.uuid4().hex}", project="lost-refresh-response",
    )
    store = GrantStore(object(), tenant=authority.tenant, project=authority.project, authority_store=authority)
    try:
        await authority.ensure_schema()
        held = await store.create_refresh_token(
            client_id="client-w408", sub="user-w408", scopes=["records:read"],
            registry_access_id="aut_w408", card_kind="automation",
        )
        transport = _LostResponseTransport(store)
        yield pool, authority, OAuthClient(transport=transport), transport, held
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
        await pool.close()


async def _refresh(client: OAuthClient, token: str):
    return await client.refresh(metadata=_metadata(), client=_registration(), resource=None, refresh_token=token)


async def _family_state(pool, authority) -> str:
    async with pool.acquire() as connection:
        return await connection.fetchval(
            f"SELECT state FROM {authority.schema}.connection_hub_oauth_credential_families LIMIT 1"
        )


@pytest.mark.asyncio
async def test_a_lost_refresh_response_makes_the_next_refresh_revoke_the_family() -> None:
    """The live sequence: ReadError after the server rotated, then reuse, then unknown."""

    async with _authority() as (pool, authority, client, transport, held):
        # One ordinary renewal: the client holds the rotated token.
        held = (await _refresh(client, held)).refresh_token
        assert transport.server_outcomes == ["rotated"]

        # The response of the next renewal is lost after the server rotated.
        transport.fail_next = "read_error"
        with pytest.raises(Exception) as lost:
            await _refresh(client, held)
        assert getattr(lost.value, "details", {}).get("failure_kind") == "ReadError"
        assert transport.server_outcomes == ["rotated", "rotated"]

        # The client still holds the token it sent, now consumed. Presenting
        # it again is reuse: the whole family is revoked.
        with pytest.raises(Exception):
            await _refresh(client, held)
        assert transport.server_outcomes[-1] == "reuse_detected_family_revoked"

        # Every later renewal is refused as unknown (the live 06:32 to 09:35Z attempts).
        with pytest.raises(Exception):
            await _refresh(client, held)
        assert transport.server_outcomes[-1] == "refresh_token_unknown"
        assert await _family_state(pool, authority) == "revoked"


@pytest.mark.asyncio
async def test_a_connect_failure_before_send_leaves_the_credential_usable() -> None:
    """Nothing reached the server, so the same token renews on the next try."""

    async with _authority() as (pool, authority, client, transport, held):
        transport.fail_next = "connect_error"
        with pytest.raises(Exception) as failed:
            await _refresh(client, held)
        assert getattr(failed.value, "details", {}).get("failure_kind") == "ConnectError"
        assert transport.server_outcomes == []
        renewed = await _refresh(client, held)
        assert renewed.refresh_token and renewed.refresh_token != held
        assert transport.server_outcomes == ["rotated"]
        assert await _family_state(pool, authority) == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("moment, family_after_retry", [("cancel_before_send", "active"), ("cancel_after_commit", "revoked")])
async def test_a_cancelled_refresh_is_harmless_before_send_and_fatal_after_the_commit(moment, family_after_retry) -> None:
    async with _authority() as (pool, authority, client, transport, held):
        transport.fail_next = moment
        with pytest.raises(asyncio.CancelledError):
            await _refresh(client, held)
        try:
            await _refresh(client, held)
        except Exception:
            pass
        assert await _family_state(pool, authority) == family_after_retry


@pytest.mark.asyncio
async def test_a_native_store_failure_after_a_delivered_response_is_the_same_loss() -> None:
    """The response arrived, but the rotated token was never stored: the client still holds the consumed one."""

    async with _authority() as (pool, authority, client, transport, held):
        delivered = await _refresh(client, held)
        assert delivered.refresh_token != held
        # The store write failed, so the client's credential is still ``held``.
        with pytest.raises(Exception):
            await _refresh(client, held)
        assert transport.server_outcomes[-1] == "reuse_detected_family_revoked"
        assert await _family_state(pool, authority) == "revoked"

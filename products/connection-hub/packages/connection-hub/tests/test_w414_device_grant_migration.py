"""W414: device login is built in for every public native dynamic client.

Operator ruling, 2026-09-30: every new client registration includes the device
grant, and every existing client gains it by a release migration, so
`pb worker authorize <profile> --device` works for an existing profile with the
same client and Card. Only public native dynamic clients are touched, and only
their grant list.

Rollout order: the grant may never reach a handler that does not check Card
continuity. The checked handler derives the grant itself
(`client_holds_device_grant`); the stored migration is an explicit release
step, never part of schema setup, and a pre-W414 row reads unchanged. Old
handlers therefore keep refusing until the release runs it after every process
is checked. Identifiers are synthetic.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
)
from connection_hub.delegated_credentials.oauth.clients import (
    client_from_record,
    client_holds_device_grant,
    public_native_grant_types,
)
from connection_hub.delegated_credentials.oauth.device import DEVICE_GRANT_TYPE
from connection_hub.delegated_credentials.oauth.store import GrantStore

BROWSER_ONLY = ["authorization_code", "refresh_token"]


def test_a_public_native_client_always_holds_the_device_grant() -> None:
    assert public_native_grant_types(
        BROWSER_ONLY, application_type="native", token_endpoint_auth_method="none"
    ) == [*BROWSER_ONLY, DEVICE_GRANT_TYPE]
    # Already present: unchanged, no duplicate.
    assert public_native_grant_types(
        [*BROWSER_ONLY, DEVICE_GRANT_TYPE], application_type="native", token_endpoint_auth_method="none"
    ) == [*BROWSER_ONLY, DEVICE_GRANT_TYPE]
    # A web client, or a client that authenticates, keeps exactly what it registered.
    assert public_native_grant_types(
        BROWSER_ONLY, application_type="web", token_endpoint_auth_method="none"
    ) == BROWSER_ONLY
    assert public_native_grant_types(
        BROWSER_ONLY, application_type="native", token_endpoint_auth_method="client_secret_basic"
    ) == BROWSER_ONLY


class _Redis:
    """Enough of Redis for dynamic client registration and its sliding read."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def eval(self, _script, _count, key, *_args):
        return self.values.get(key)


@pytest.mark.asyncio
async def test_registration_adds_the_device_grant_on_the_redis_path_and_reads_old_rows_with_it() -> None:
    redis = _Redis()
    store = GrantStore(redis, tenant="w414", project="device")

    native = await store.register_client(redirect_uris=["http://127.0.0.1/callback"], grant_types=BROWSER_ONLY)
    web = await store.register_client(
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"], grant_types=BROWSER_ONLY, application_type="web"
    )
    assert DEVICE_GRANT_TYPE in native["grant_types"]
    assert web["grant_types"] == BROWSER_ONLY

    # A registration stored before W414 reads unchanged, so a handler older
    # than the continuity check keeps refusing it; the checked handler derives
    # the grant for it.
    old_key = next(key for key in redis.values if native["client_id"] in key)
    stored = json.loads(redis.values[old_key])
    stored["grant_types"] = BROWSER_ONLY
    redis.values[old_key] = json.dumps(stored)
    read = await store.get_client_record(native["client_id"])
    assert read["grant_types"] == BROWSER_ONLY
    assert client_holds_device_grant(client_from_record(read))
    web_read = await store.get_client_record(web["client_id"])
    assert web_read["grant_types"] == BROWSER_ONLY
    assert not client_holds_device_grant(client_from_record(web_read))


@asynccontextmanager
async def _authority():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    authority = PostgresOAuthAuthorityStore(
        pg_pool=pool, tenant=f"w414-{uuid.uuid4().hex}", project="device-grant",
    )
    try:
        await authority.ensure_schema()
        yield pool, authority
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
        await pool.close()


async def _insert(pool, authority, client_id, *, grants, application_type="native", auth="none"):
    async with pool.acquire() as connection:
        await connection.execute(
            f"""INSERT INTO {authority.schema}.connection_hub_oauth_clients
                (client_id, tenant, project, redirect_uris, grant_types,
                 token_endpoint_auth_method, application_type, metadata, revision, expires_at)
                VALUES ($1, $2, $3, '["http://127.0.0.1/callback"]'::jsonb, ($4::text)::jsonb,
                        $5, $6, '{{"client_name": "pb"}}'::jsonb, 3, now() + interval '30 days')""",
            client_id, authority.tenant, authority.project, json.dumps(grants), auth, application_type,
        )


async def _rows(pool, authority):
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""SELECT client_id, redirect_uris, grant_types, token_endpoint_auth_method,
                       application_type, metadata, revision, created_at, last_used_at,
                       expires_at, retired_at
                FROM {authority.schema}.connection_hub_oauth_clients ORDER BY client_id"""
        )
    return {row["client_id"]: dict(row) for row in rows}


@pytest.mark.asyncio
async def test_the_migration_adds_only_the_device_grant_to_existing_public_native_clients() -> None:
    async with _authority() as (pool, authority):
        # The Infra shape: an existing browser-only public native DCR client.
        await _insert(pool, authority, "dcr-browser-only", grants=BROWSER_ONLY)
        await _insert(pool, authority, "dcr-device-already", grants=[*BROWSER_ONLY, DEVICE_GRANT_TYPE])
        await _insert(pool, authority, "dcr-web-client", grants=BROWSER_ONLY, application_type="web")
        await _insert(pool, authority, "provisioned-native", grants=BROWSER_ONLY)
        before = await _rows(pool, authority)

        # Schema setup never migrates: a handler older than the continuity
        # check must not see the grant during a mixed-process rollout.
        await authority.ensure_schema()
        assert await _rows(pool, authority) == before

        changed = await authority.grant_device_to_public_native_clients()
        after = await _rows(pool, authority)

        assert changed == 1
        assert json.loads(after["dcr-browser-only"]["grant_types"]) == [*BROWSER_ONLY, DEVICE_GRANT_TYPE]
        # Nothing else about the migrated row changed: same client, same revision, same TTL.
        for field in ("redirect_uris", "token_endpoint_auth_method", "application_type", "metadata",
                      "revision", "created_at", "last_used_at", "expires_at", "retired_at"):
            assert after["dcr-browser-only"][field] == before["dcr-browser-only"][field], field
        # Every other client is untouched.
        for client_id in ("dcr-device-already", "dcr-web-client", "provisioned-native"):
            assert after[client_id] == before[client_id], client_id

        # Idempotent: a second run changes nothing.
        assert await authority.grant_device_to_public_native_clients() == 0
        assert await _rows(pool, authority) == after


@pytest.mark.asyncio
async def test_a_new_registration_through_the_durable_store_holds_the_device_grant() -> None:
    async with _authority() as (pool, authority):
        store = GrantStore(object(), tenant=authority.tenant, project=authority.project, authority_store=authority)
        native = await store.register_client(redirect_uris=["http://127.0.0.1/callback"], grant_types=BROWSER_ONLY)
        stored = await store.get_client_record(native["client_id"])
        assert stored["grant_types"] == [*BROWSER_ONLY, DEVICE_GRANT_TYPE]


# W414, operator ruling 2026-09-30: device login re-authorizes an existing Card
# only with continuity proof, the Card's last refresh token, checked against the
# Card's own credential families for the same client. Revoked families still
# count: the machine recovering after a revocation (the W408 shape) holds the
# last token of a family that is no longer active.
@pytest.mark.asyncio
async def test_card_continuity_is_proven_by_any_generation_of_the_card_s_families() -> None:
    async with _authority() as (_pool, authority):
        record = {"registry_access_id": "con_card_a", "client_id": "dcr-browser-only", "sub": "google:owner"}
        first = await authority.create_refresh_token(record, ttl_seconds=600)
        last = await authority.rotate_refresh_token(first, record, ttl_seconds=600)
        assert last
        other_card = await authority.create_refresh_token(
            {**record, "registry_access_id": "con_card_b"}, ttl_seconds=600
        )
        other_client = await authority.create_refresh_token(
            {**record, "client_id": "dcr-stranger"}, ttl_seconds=600
        )

        async def proven(token, client_id="dcr-browser-only", access_id="con_card_a"):
            return await authority.card_continuity_proven(
                refresh_token=token, client_id=client_id, access_id=access_id
            )

        assert await proven(last)
        assert await proven(first)
        # The Card's credentials revoked, as the W408 reuse revocation does.
        assert await authority.revoke_card_credentials("con_card_a")
        assert await proven(last)

        assert not await proven("")
        assert not await proven("a-guessed-refresh-token")
        assert not await proven(other_card)
        assert not await proven(other_client)
        assert not await proven(last, client_id="dcr-stranger")
        assert not await proven(last, access_id="con_card_b")
        assert not await proven(last, access_id="")


@pytest.mark.asyncio
async def test_card_continuity_is_bound_to_its_tenant_and_project() -> None:
    async with _authority() as (pool, authority):
        record = {"registry_access_id": "con_card_a", "client_id": "dcr-browser-only", "sub": "google:owner"}
        token = await authority.create_refresh_token(record, ttl_seconds=600)
        proof = {"refresh_token": token, "client_id": "dcr-browser-only", "access_id": "con_card_a"}
        assert await authority.card_continuity_proven(**proof)

        # The same proof presented in another tenant or project proves nothing.
        for tenant, project in ((f"{authority.tenant}-other", authority.project),
                                (authority.tenant, f"{authority.project}-other")):
            other = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=tenant, project=project)
            await other.ensure_schema()
            try:
                assert not await other.card_continuity_proven(**proof), (tenant, project)
            finally:
                async with pool.acquire() as connection:
                    await connection.execute(f"DROP SCHEMA IF EXISTS {other.schema} CASCADE")

        # Within the schema, a family recorded for another scope does not count.
        async with pool.acquire() as connection:
            await connection.execute(
                f"UPDATE {authority.schema}.connection_hub_oauth_credential_families SET tenant = 'elsewhere'"
            )
        assert not await authority.card_continuity_proven(**proof)


@pytest.mark.asyncio
async def test_without_a_durable_authority_no_card_continuity_is_proven() -> None:
    store = GrantStore(_Redis(), tenant="w414", project="device")
    assert not await store.card_continuity_proven(
        refresh_token="anything", client_id="dcr-any", access_id="con_any"
    )


# The live shape on a PostgreSQL authority (2026-09-30): a browser-first public
# native client registered before W414, whose Card's family was revoked by the
# W408 lost-response sequence. The machine kept its previous refresh token
# (the response carrying the successor never arrived) and presents it again;
# reuse detection revokes the family. That kept token still proves continuity,
# and the unmigrated client is device-capable only through the checked handler.
@pytest.mark.asyncio
async def test_the_live_shape_recovers_with_the_token_the_machine_kept() -> None:
    from connection_hub.delegated_credentials.oauth.store import RefreshTokenReuseDetected

    async with _authority() as (pool, authority):
        await _insert(pool, authority, "dcr-browser-first", grants=BROWSER_ONLY)
        record = {"registry_access_id": "con_infra_card", "client_id": "dcr-browser-first", "sub": "google:owner"}
        kept = await authority.create_refresh_token(record, ttl_seconds=600)
        successor = await authority.rotate_refresh_token(kept, record, ttl_seconds=600)
        assert successor  # committed by the server; its response was lost
        with pytest.raises(RefreshTokenReuseDetected):
            await authority.get_refresh_token_state(kept)
        assert await authority.get_refresh_token_state(successor) is None  # the family is revoked

        stored = await GrantStore(
            object(), tenant=authority.tenant, project=authority.project, authority_store=authority
        ).get_client_record("dcr-browser-first")
        assert stored["grant_types"] == BROWSER_ONLY  # no stored change before the release step
        assert client_holds_device_grant(client_from_record(stored))

        assert await authority.card_continuity_proven(
            refresh_token=kept, client_id="dcr-browser-first", access_id="con_infra_card"
        )
        assert not await authority.card_continuity_proven(
            refresh_token=kept, client_id="dcr-browser-first", access_id="con_other_card"
        )


def test_the_continuity_refusal_reaches_the_client_as_a_registered_code() -> None:
    """The client carries only registered OAuth error codes; without this one
    the named continuity refusals would never be raised against a real server."""

    from connection_hub.caller.authorization.discovery import _token_error_code
    from connection_hub.delegated_credentials.oauth.device import DEVICE_TERMINAL_ERRORS

    body = json.dumps({"error": "card_continuity_required", "error_description": "echo"}).encode()
    assert _token_error_code(body) == "card_continuity_required"
    assert "card_continuity_required" in DEVICE_TERMINAL_ERRORS

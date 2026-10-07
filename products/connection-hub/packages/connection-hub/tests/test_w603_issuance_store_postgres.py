"""W603: an original issuance's plan and reservations on real PostgreSQL (DSN-gated).

A reserved credential is in no table a reader looks at; only an activation
under its decision's effect makes it usable, and a superseded Card, an ABORT
or a missed deadline leaves nothing usable. Identifiers and bearers are
synthetic; bearers are only ever passed to the store as SHA-256 digests.
"""

from __future__ import annotations

import os
import secrets
import uuid
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.oauth.authority_store import PostgresOAuthAuthorityStore
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256
from connection_hub.delegated_credentials.oauth.issuance_store import IssuanceStoreRefused

ACCESS_ID = "aut_w603"
GRANTOR = "user-w603"
CLIENT = "client-w603"


@asynccontextmanager
async def _authority():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    authority = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=f"w603-{uuid.uuid4().hex}", project="issuance")
    try:
        await authority.ensure_schema()
        yield pool, authority
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE")
        await pool.close()


def _hex() -> str:
    return secrets.token_hex(32)


async def _plan(authority, *, window: int = 300, card_ttl: int = 3600, revision: int = 2):
    now = await authority.issuance_clock()
    request, transaction = _hex(), _hex()
    plan = {"slots": ["access", "refresh"], "access_id": ACCESS_ID, "grantor_subject": GRANTOR,
            "client_id": CLIENT, "candidate_revision": revision, "expires_at": now + card_ttl}
    await authority.put_issuance_plan(decision_request_id=request, original_input_digest="a" * 64,
                                      plan=plan, reserved_until=now + window)
    await authority.bind_issuance_plan_transaction(decision_request_id=request, transaction_id=transaction)
    return request, transaction, plan, now


def _record(**extra):
    return {"sub": GRANTOR, "client_id": CLIENT, "registry_access_id": ACCESS_ID, "card_kind": "automation",
            "identity_scope": "", **extra}


async def _reserve_both(authority, transaction, *, ttl: int = 600):
    tokens = {slot: secrets.token_urlsafe(32) for slot in ("access", "refresh")}
    for slot, token in tokens.items():
        await authority.reserve_issued_credential(transaction_id=transaction, slot=slot,
                                                  token_sha256=bearer_sha256(token), record=_record(), ttl_seconds=ttl)
    return tokens


async def _usable(authority, tokens) -> dict[str, bool]:
    return {"access": await authority.get_access_grant_record(tokens["access"]) is not None,
            "refresh": await authority.get_refresh_token_state(tokens["refresh"]) is not None}


@pytest.mark.asyncio
async def test_the_first_plan_wins_and_a_changed_original_refuses():
    async with _authority() as (_pool, authority):
        request, transaction, plan, now = await _plan(authority)
        again = await authority.put_issuance_plan(decision_request_id=request, original_input_digest="a" * 64,
                                                  plan={**plan, "expires_at": now + 99999}, reserved_until=now + 1)
        assert again == {"plan": plan, "transaction_id": transaction}  # the stored plan, not the replay's
        with pytest.raises(IssuanceStoreRefused, match="issuance_replay_changed"):
            await authority.put_issuance_plan(decision_request_id=request, original_input_digest="b" * 64,
                                              plan=plan, reserved_until=now + 300)
        await authority.bind_issuance_plan_transaction(decision_request_id=request, transaction_id=transaction)
        with pytest.raises(IssuanceStoreRefused, match="issuance_plan_transaction_conflict"):
            await authority.bind_issuance_plan_transaction(decision_request_id=request, transaction_id=_hex())
        assert (await authority.read_issuance_plan(transaction))["plan"] == plan


@pytest.mark.asyncio
async def test_a_reservation_copies_the_plan_and_is_usable_nowhere():
    async with _authority() as (pool, authority):
        _request, transaction, plan, _now = await _plan(authority)
        tokens = await _reserve_both(authority, transaction)
        assert await _usable(authority, tokens) == {"access": False, "refresh": False}
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT slot, registry_access_id, subject, client_id, card_revision, state, record::text AS record "
                f"FROM {authority.schema}.connection_hub_oauth_issuance_reservations ORDER BY slot")
            families = await connection.fetchval(
                f"SELECT count(*) FROM {authority.schema}.connection_hub_oauth_credential_families")
        assert families == 0
        assert [(r["slot"], r["registry_access_id"], r["subject"], r["client_id"], r["card_revision"], r["state"])
                for r in rows] == [("access", ACCESS_ID, GRANTOR, CLIENT, 2, "reserved"),
                                   ("refresh", ACCESS_ID, GRANTOR, CLIENT, 2, "reserved")]
        assert all(tokens[r["slot"]] not in r["record"] for r in rows)  # only the digest was ever stored


@pytest.mark.asyncio
async def test_a_reservation_replays_exactly_and_refuses_any_change():
    async with _authority() as (_pool, authority):
        _request, transaction, _plan_value, _now = await _plan(authority)
        digest = bearer_sha256(secrets.token_urlsafe(32))
        first = await authority.reserve_issued_credential(transaction_id=transaction, slot="access",
                                                          token_sha256=digest, record=_record(), ttl_seconds=600)
        assert await authority.reserve_issued_credential(transaction_id=transaction, slot="access",
                                                         token_sha256=digest, record=_record(),
                                                         ttl_seconds=600) == first
        for change in ({"token_sha256": bearer_sha256(secrets.token_urlsafe(32)), "record": _record(),
                        "ttl_seconds": 600},
                       {"token_sha256": digest, "record": _record(scopes=["more"]), "ttl_seconds": 600},
                       {"token_sha256": digest, "record": _record(), "ttl_seconds": 601}):
            with pytest.raises(IssuanceStoreRefused, match="reservation_replay_changed"):
                await authority.reserve_issued_credential(transaction_id=transaction, slot="access", **change)
        with pytest.raises(IssuanceStoreRefused, match="reservation_slot_undeclared"):
            await authority.reserve_issued_credential(transaction_id=transaction, slot="id_token",
                                                      token_sha256=digest, record=_record(), ttl_seconds=600)
        with pytest.raises(IssuanceStoreRefused, match="issuance_plan_unknown"):
            await authority.reserve_issued_credential(transaction_id=_hex(), slot="access",
                                                      token_sha256=digest, record=_record(), ttl_seconds=600)
        for ttl in (0, -1, 400 * 86400 + 1, True):
            with pytest.raises(IssuanceStoreRefused, match="reservation_ttl_invalid"):
                await authority.reserve_issued_credential(transaction_id=transaction, slot="refresh",
                                                          token_sha256=digest, record=_record(), ttl_seconds=ttl)


@pytest.mark.asyncio
async def test_a_closed_window_refuses_a_reservation_and_a_stage():
    async with _authority() as (pool, authority):
        _request, transaction, _plan_value, _now = await _plan(authority)
        await _reserve_both(authority, transaction)
        async with pool.acquire() as connection:
            for table in ("connection_hub_oauth_issuance_plans", "connection_hub_oauth_issuance_reservations"):
                await connection.execute(f"UPDATE {authority.schema}.{table} "
                                         f"SET reserved_until = clock_timestamp() - interval '1 second'")
        with pytest.raises(IssuanceStoreRefused, match="reservation_window_closed"):
            await authority.bind_issued_credential(transaction_id=transaction, slot="access", effect_digest=_hex(),
                                                   access_id=ACCESS_ID, card_revision=2)
        _r2, other, _p2, _n2 = await _plan(authority, window=-1)
        with pytest.raises(IssuanceStoreRefused, match="reservation_window_closed"):
            await authority.reserve_issued_credential(transaction_id=other, slot="access",
                                                      token_sha256=bearer_sha256("x"), record=_record(),
                                                      ttl_seconds=600)
        # Only never-bound reservations expire, and the sweep is idempotent.
        assert await authority.expire_issuance_reservations() == 2
        assert await authority.expire_issuance_reservations() == 0
        assert {slot: row["state"] for slot, row in (await authority.issuance_reservations(transaction)).items()} \
            == {"access": "expired", "refresh": "expired"}


@pytest.mark.asyncio
async def test_activation_after_stage_makes_the_original_credentials_usable_once():
    async with _authority() as (pool, authority):
        _request, transaction, plan, now = await _plan(authority, card_ttl=120)
        tokens = await _reserve_both(authority, transaction, ttl=3600)
        effects = {slot: _hex() for slot in tokens}
        for slot in tokens:
            with pytest.raises(IssuanceStoreRefused, match="reservation_binding_mismatch"):
                await authority.bind_issued_credential(transaction_id=transaction, slot=slot,
                                                       effect_digest=effects[slot], access_id=ACCESS_ID,
                                                       card_revision=3)
            for _ in range(2):
                assert await authority.bind_issued_credential(
                    transaction_id=transaction, slot=slot, effect_digest=effects[slot], access_id=ACCESS_ID,
                    card_revision=2) == "bound"
            with pytest.raises(IssuanceStoreRefused, match="reservation_binding_mismatch"):
                await authority.bind_issued_credential(transaction_id=transaction, slot=slot, effect_digest=_hex(),
                                                       access_id=ACCESS_ID, card_revision=2)
        assert await _usable(authority, tokens) == {"access": False, "refresh": False}  # bound is not usable
        with pytest.raises(IssuanceStoreRefused, match="reservation_bound"):
            await authority.release_issued_credential(transaction_id=transaction, slot="access", unbound_only=True)
        for slot in tokens:
            with pytest.raises(IssuanceStoreRefused, match="reservation_binding_mismatch"):
                await authority.activate_issued_credential(transaction_id=transaction, slot=slot,
                                                           effect_digest=_hex(), card_live=True)
            for _ in range(2):
                assert await authority.activate_issued_credential(
                    transaction_id=transaction, slot=slot, effect_digest=effects[slot], card_live=True) == "applied"
        assert await _usable(authority, tokens) == {"access": True, "refresh": True}
        async with pool.acquire() as connection:
            family = await connection.fetchrow(
                f"SELECT registry_access_id, subject, client_id, card_revision, "
                f"extract(epoch from expires_at)::bigint AS expires, extract(epoch from cap_expires_at)::bigint AS cap "
                f"FROM {authority.schema}.connection_hub_oauth_credential_families")
            binding = await connection.fetchrow(
                f"SELECT card_revision, extract(epoch from expires_at)::bigint AS expires "
                f"FROM {authority.schema}.connection_hub_oauth_access_bindings")
            count = await connection.fetchval(
                f"SELECT count(*) FROM {authority.schema}.connection_hub_oauth_credential_families")
        assert count == 1  # the replay inserted nothing more
        # The token asked for an hour; the Card ends in two minutes, and the cap wins.
        assert family["expires"] == family["cap"] == plan["expires_at"]
        assert (family["registry_access_id"], family["subject"], family["client_id"], family["card_revision"]) \
            == (ACCESS_ID, GRANTOR, CLIENT, 2)
        assert (binding["card_revision"], binding["expires"]) == (2, plan["expires_at"])
        with pytest.raises(IssuanceStoreRefused, match="reservation_activated"):
            await authority.release_issued_credential(transaction_id=transaction, slot="access")
        assert {slot: (row["state"], row["outcome"]) for slot, row in
                (await authority.issuance_reservations(transaction)).items()} \
            == {"access": ("activated", "applied"), "refresh": ("activated", "applied")}


@pytest.mark.asyncio
async def test_a_superseded_card_activates_nothing_even_with_no_family():
    async with _authority() as (pool, authority):
        _request, transaction, _plan_value, _now = await _plan(authority)
        tokens = await _reserve_both(authority, transaction)
        effects = {slot: _hex() for slot in tokens}
        for slot in tokens:
            await authority.bind_issued_credential(transaction_id=transaction, slot=slot, effect_digest=effects[slot],
                                                   access_id=ACCESS_ID, card_revision=2)
            for _ in range(2):
                assert await authority.activate_issued_credential(
                    transaction_id=transaction, slot=slot, effect_digest=effects[slot], card_live=False) == "superseded"
            # The pinned outcome stands even if a later caller reads the Card live.
            assert await authority.activate_issued_credential(
                transaction_id=transaction, slot=slot, effect_digest=effects[slot], card_live=True) == "superseded"
        assert await _usable(authority, tokens) == {"access": False, "refresh": False}
        async with pool.acquire() as connection:
            assert await connection.fetchval(
                f"SELECT count(*) FROM {authority.schema}.connection_hub_oauth_credential_families") == 0


@pytest.mark.asyncio
async def test_an_abort_releases_bound_and_reserved_and_nothing_activates_afterwards():
    async with _authority() as (_pool, authority):
        _request, transaction, _plan_value, _now = await _plan(authority)
        tokens = await _reserve_both(authority, transaction)
        effect = _hex()
        await authority.bind_issued_credential(transaction_id=transaction, slot="access", effect_digest=effect,
                                               access_id=ACCESS_ID, card_revision=2)
        # access was bound by STAGE; refresh is a reservation that raced the ABORT and was never bound.
        for slot in tokens:
            for _ in range(2):
                assert await authority.release_issued_credential(transaction_id=transaction, slot=slot) == "released"
        with pytest.raises(IssuanceStoreRefused, match="reservation_binding_mismatch"):
            await authority.activate_issued_credential(transaction_id=transaction, slot="access",
                                                       effect_digest=effect, card_live=True)
        with pytest.raises(IssuanceStoreRefused, match="reservation_binding_mismatch"):
            await authority.bind_issued_credential(transaction_id=transaction, slot="refresh", effect_digest=_hex(),
                                                   access_id=ACCESS_ID, card_revision=2)
        assert await _usable(authority, tokens) == {"access": False, "refresh": False}
        assert await authority.release_issued_credential(transaction_id=_hex(), slot="access") == "absent"
        assert await authority.expire_issuance_reservations() == 0  # released rows are never re-expired

"""W603: an original issuance survives a process killed at every cut, and a fresh process recovers it.

Three processes per case, sharing only durable state (the Card store on disk,
the real PostgresDecisionStore and the PostgreSQL OAuth authority on a
disposable PostgreSQL, the Redis credential handles):

1. this test names the tenant and the directory (a re-consent case first
   commits the Card's first issuance in a child of its own);
2. a child composes, begins the issuance, reserves both minted credentials,
   prints their digests and the plan, and SIGKILLs itself at the cut inside
   ``complete_oauth_issuance``;
3. a fresh child composes again, runs ``recover_card_transactions`` past the
   intent's expiry and then the SDK's retry, ``complete_oauth_issuance``.

An undecided cut is presumed aborted: no Card change and nothing usable. A
committed cut finishes: the ORIGINAL two credentials (the same digests) are
usable, the Card is at its planned revision, nothing is in doubt, and no
credential was minted again. The SDK side is synthetic (bearers made by the
test child, passed to the Hub only as digests).

Skipped unless ``CONNECTION_HUB_TEST_POSTGRES_DSN_FILE`` (preferred) or
``CONNECTION_HUB_TEST_POSTGRES_DSN`` names a disposable database, and
``REDIS_URL`` a disposable Redis.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

DSN_ENV = "CONNECTION_HUB_TEST_POSTGRES_DSN"
DSN_FILE_ENV = "CONNECTION_HUB_TEST_POSTGRES_DSN_FILE"
CUTS = ("before_stage", "after_stage", "after_commit_before_activation", "after_activation_before_finish")
UNDECIDED_CUTS = ("before_stage", "after_stage")
INTENT_TTL = 3  # seconds: the reservations are made well inside it; recovery waits past it
GRANTOR = "user-w603-kill"
CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"
RESOURCE = "https://host/api/mcp/memories"
SCOPES = ["memories:read"]


def _dsn() -> str:
    path = os.environ.get(DSN_FILE_ENV, "")
    if path:
        return pathlib.Path(path).read_text().strip()
    return os.environ.get(DSN_ENV, "")


pytestmark = pytest.mark.skipif(
    not (os.environ.get(DSN_FILE_ENV) or os.environ.get(DSN_ENV)) or not os.environ.get("REDIS_URL"),
    reason=f"needs a disposable PostgreSQL ({DSN_FILE_ENV} or {DSN_ENV}) and REDIS_URL")


async def _compose(root: pathlib.Path, pool, redis_client, *, tenant: str):
    """What every process builds over the durable state: the production binding plus the issuance store."""
    from contextlib import asynccontextmanager

    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.cards.credential_handles import RedisCardCredentialHandleStore
    from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from connection_hub.delegated_credentials.oauth.authority_store import PostgresOAuthAuthorityStore
    from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
    from connection_hub.delegated_credentials.oauth.store import GrantStore
    from test_card_service import _Cache
    from test_project_control_cards import _Redis
    from test_resident_profile_cards import _Catalog, _connections

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield  # one process at a time in this test

    authority = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=tenant, project="w603-kill")
    await authority.ensure_schema()
    grants = GrantStore(object(), tenant=tenant, project="w603-kill", authority_store=authority)
    store = BundleStorageDelegatedCardStore(root / "cards")
    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    handles = RedisCardCredentialHandleStore(redis_client, tenant=tenant, project="w603-kill")
    persistence = DurableCardPersistence(redis=redis_client, tenant=tenant, project="w603-kill", card_store=store,
                                         mutation_lock=mutation_lock, credential_handles=handles)
    persistence._cards = cards  # only the serving projection is fake
    connections = _connections()
    service = AutomationAccessService(redis=_Redis(), tenant=tenant, project="w603-kill",
                                      config=oauth_delegated_config_from_connections(connections),
                                      catalog_resolver=_Catalog(connections), grant_store=grants,
                                      card_persistence=persistence)
    decisions = await composition.postgres_decision_store(pool, tenant=tenant, project="w603-kill")
    coordinator = composition.bind_card_transactions(service, persistence=persistence, decisions=decisions,
                                                     grant_store=grants, policies=None, issuance_store=authority,
                                                     credential_handles=handles)
    bound, intents, decisions_, _ = service._card_coordinator
    service._card_coordinator = (bound, intents, decisions_, INTENT_TTL)
    return SimpleNamespace(service=service, store=store, authority=authority, decisions=decisions,
                           coordinator=coordinator, cards=cards)


def _records(w, plan):
    from connection_hub.delegated_credentials.oauth.authority import build_delegated_client_credential

    # The SDK copies the plan's authority snapshot (declared keys) into both records and the envelope.
    grants = {key: list(items) for key, items in plan.resource_grants.items()}
    operations_map = {key: list(items) for key, items in plan.resource_operations.items()}
    credential = build_delegated_client_credential(
        grantor_subject=GRANTOR, client_id=CLIENT, scopes=list(plan.scopes), tenant=w.authority.tenant,
        project=w.authority.project, expires_in=3600, resources=list(grants), resource_grants=grants,
        resource_operations=operations_map, operations=list(plan.operations)).to_dict()
    return {"access": {"operations": list(plan.operations), "resource_grants": grants,
                       "resource_operations": operations_map, "credential": credential,
                       "grantor_authority": {}, "delegation_edges": [], "named_services": {},
                       "registry_access_id": plan.access_id},
            "refresh": {"registry_access_id": plan.access_id, "card_kind": "", "client_id": CLIENT, "sub": GRANTOR,
                        "scopes": list(plan.scopes), "operations": list(plan.operations), "resource_grants": grants,
                        "resource_operations": operations_map,
                        "resource": RESOURCE, "identity_scope": "", "credential": credential}}


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def _open():
    import asyncpg
    import redis.asyncio as redis_asyncio

    return (await asyncpg.create_pool(_dsn(), min_size=1, max_size=4),
            redis_asyncio.from_url(os.environ["REDIS_URL"]))


async def _child_issue(root: pathlib.Path, tenant: str, request: str, cut: str) -> None:
    from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256

    pool, redis_client = await _open()
    w = await _compose(root, pool, redis_client, tenant=tenant)
    plan = await w.service.begin_oauth_issuance(grantor_subject=GRANTOR, client_id=CLIENT,
                                                original_request_id=request, client_label="Claude Code",
                                                scopes=SCOPES, resource=RESOURCE)
    records = _records(w, plan)
    digests = {}
    for slot in plan.slots:
        digests[slot] = bearer_sha256(secrets.token_urlsafe(32))
        await w.service.reserve_oauth_issuance(plan=plan, slot=slot, token_sha256=digests[slot],
                                               record=records[slot], ttl_seconds=3600)
    print(json.dumps({"plan": plan.to_dict(), "digests": digests}), flush=True)

    def dying(target, name):
        async def replacement(*_args, **_kwargs):
            _die()

        setattr(target, name, replacement)

    if cut == "none":
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        print(json.dumps({"result": result.to_dict()}), flush=True)
        return
    if cut == "before_stage":  # begun and reserved, nothing prepared
        dying(w.coordinator, "prepare_existing")
    elif cut == "after_stage":  # prepared (reservations bound), no decision
        dying(w.coordinator, "decide")
    elif cut == "after_commit_before_activation":  # COMMIT recorded, no credential active
        dying(w.authority, "activate_issued_credential")
    elif cut == "after_activation_before_finish":  # both active, the Hub's FINISH not recorded
        dying(w.decisions, "record_finished")
    else:
        raise SystemExit(f"unknown cut {cut}")
    await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
    raise SystemExit(3)  # the cut was never reached


async def _child_recover(root: pathlib.Path, tenant: str, transaction_id: str) -> None:
    from connection_hub.delegated_credentials.cards.composition import recover_card_transactions

    pool, redis_client = await _open()
    w = await _compose(root, pool, redis_client, tenant=tenant)
    await asyncio.sleep(INTENT_TTL + 0.5)  # past the intent: an undecided transaction is presumed aborted
    recovered = await recover_card_transactions(w.coordinator, limit=10)
    result = await w.service.complete_oauth_issuance(transaction_id=transaction_id)  # the SDK's retry
    print(json.dumps({"recovered": recovered, "result": result.to_dict()}, default=str), flush=True)


def _run_child(*args: str) -> subprocess.CompletedProcess:
    here = pathlib.Path(__file__).resolve().parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(here), os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _last_json(text: str) -> dict:
    return json.loads([line for line in text.splitlines() if line.startswith("{")][-1])


async def _active(pool, schema: str, digests: dict[str, str]) -> dict[str, bool]:
    async with pool.acquire() as connection:
        access = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.connection_hub_oauth_access_bindings "
            f"WHERE token_sha256 = $1 AND state = 'active' AND expires_at > now()", digests["access"])
        refresh = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.connection_hub_oauth_refresh_generations "
            f"WHERE token_sha256 = $1 AND state = 'active' AND expires_at > now()", digests["refresh"])
    return {"access": access == 1, "refresh": refresh == 1}


@pytest.fixture
async def database():
    import asyncpg

    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.oauth.authority_schema import oauth_authority_schema

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)
    tenant = f"t{uuid.uuid4().hex[:10]}"
    try:
        yield pool, tenant
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                composition.decision_namespace(tenant=tenant, project="w603-kill"))
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {oauth_authority_schema(tenant=tenant, project='w603-kill')} CASCADE")
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", ["first", "again"])
@pytest.mark.parametrize("cut", CUTS)
async def test_an_issuance_killed_at_the_cut_is_recovered_by_a_fresh_process(tmp_path, database, consent, cut):
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.store import subject_hash_for
    from connection_hub.delegated_credentials.oauth.authority_schema import oauth_authority_schema

    pool, tenant = database
    schema = oauth_authority_schema(tenant=tenant, project="w603-kill")
    base = 0
    if consent == "again":
        first = _run_child("issue", str(tmp_path), tenant, "exchange-first", "none")
        assert first.returncode == 0, (first.stdout, first.stderr)
        assert _last_json(first.stdout)["result"]["state"] == "committed"
        base = 1

    killed = _run_child("issue", str(tmp_path), tenant, "exchange-cut", cut)
    assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stdout, killed.stderr)
    issued = _last_json(killed.stdout)
    plan, digests = issued["plan"], issued["digests"]
    assert plan["base_revision"] == base

    recovered = _run_child("recover", str(tmp_path), tenant, plan["transaction_id"])
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    result = _last_json(recovered.stdout)["result"]

    import redis.asyncio as redis_asyncio
    redis_client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    try:
        w = await _compose(tmp_path, pool, redis_client, tenant=tenant)
        assert await w.decisions.list_in_doubt(limit=10) == []
        assert await tx.list_in_doubt(w.store) == []
        found = await w.store.read_current_authority(subject_hash=subject_hash_for(GRANTOR),
                                                     access_id=plan["access_id"])
        revision = 0 if found is None else found[1].card_revision
        if cut in UNDECIDED_CUTS:
            assert result["state"] == "aborted" and result["receipt_digest"] == ""
            assert revision == base
            assert await _active(pool, schema, digests) == {"access": False, "refresh": False}
        else:
            assert result["state"] == "committed" and result["receipt_digest"]
            assert revision == plan["candidate_revision"] == base + 1
            assert {slot: o["outcome"] for slot, o in result["per_slot"].items()} \
                == {"access": "applied", "refresh": "applied"}
            # The ORIGINAL credentials, never a second mint.
            assert {slot: o["token_sha256"] for slot, o in result["per_slot"].items()} == digests
            assert await _active(pool, schema, digests) == {"access": True, "refresh": True}
            assert {slot: o["effect_digest"] for slot, o in result["per_slot"].items()} == plan["effect_digests"]
        async with pool.acquire() as connection:
            reserved = await connection.fetchval(
                f"SELECT count(*) FROM {schema}.connection_hub_oauth_issuance_reservations "
                f"WHERE transaction_id = $1", plan["transaction_id"])
        assert reserved == 2  # exactly the plan's two slots, never re-reserved
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    mode, root, tenant, *rest = sys.argv[1:]
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    if mode == "issue":
        asyncio.run(_child_issue(pathlib.Path(root), tenant, *rest))
    elif mode == "recover":
        asyncio.run(_child_recover(pathlib.Path(root), tenant, *rest))
    else:
        raise SystemExit(f"unknown mode {mode}")

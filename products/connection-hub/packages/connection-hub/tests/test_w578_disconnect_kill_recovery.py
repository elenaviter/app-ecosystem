"""W578: an account disconnect survives a process killed at every cut, and a fresh process recovers it.

claude-main's activation condition (7 October 2026, 04:22 UTC): "Run the same
cuts (before STAGE, after STAGE, after COMMIT before the delete, during the
delete, after the delete before FINISH), with: the real
`PostgresDecisionStore` on a disposable PostgreSQL 16; a durable account
backend (a file-backed user-props store is enough); the Card store on disk; a
child process SIGKILLed at each cut and a fresh process recovering."

Three processes per case, sharing only durable state:

1. this test writes the Card store, the account records and the decision
   namespace;
2. a child composes the production ``bind_card_transactions`` over them, runs
   the disconnect and SIGKILLs itself at the cut;
3. a fresh child composes again and runs ``recover_card_transactions``.

The test then reads disk and PostgreSQL only. An undecided cut is presumed
aborted (the intent's TTL is shortened to three seconds for the test): the
account, its incarnation and the Card are kept. A committed cut finishes: the
account, its index entry and its credential are gone, the Card no longer binds
it, and nothing stays fenced, held or in doubt. Both the Card group path and
the effects-only path (no Card binds the account) run every cut.

Skipped unless ``CONNECTION_HUB_TEST_POSTGRES_DSN`` names a disposable
database.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
import uuid
from dataclasses import replace

import pytest

DSN_ENV = "CONNECTION_HUB_TEST_POSTGRES_DSN"
PROVIDER, BOUND, FREE = "google", "acct-bound", "acct-free"
CUTS = ("before_stage", "after_stage", "after_commit_before_delete", "during_delete", "after_delete_before_finish")
UNDECIDED_CUTS = ("before_stage", "after_stage")
INTENT_TTL = 3  # seconds: long enough for a committed cut to decide, short enough for recovery to wait

pytestmark = pytest.mark.skipif(not os.environ.get(DSN_ENV), reason=f"needs a disposable PostgreSQL ({DSN_ENV})")


class _FileUserConfiguration:
    """A durable user-props and secrets backend: one JSON file per key, written atomically."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _path(self, kind: str, key: str, kwargs) -> pathlib.Path:
        name = json.dumps([kwargs["user_id"], kwargs["bundle_id"], key]).encode("utf-8").hex()
        return self.root / kind / f"{name}.json"

    def _read(self, path: pathlib.Path, default=None):
        try:
            return json.loads(path.read_text())["value"]
        except FileNotFoundError:
            return default

    def _write(self, path: pathlib.Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"value": value}))
        os.replace(temporary, path)

    async def get_user_prop(self, key, **kwargs):
        return self._read(self._path("props", key, kwargs), kwargs.get("default"))

    async def set_user_prop(self, key, value, **kwargs):
        self._write(self._path("props", key, kwargs), value)

    async def delete_user_prop(self, key, **kwargs):
        self._path("props", key, kwargs).unlink(missing_ok=True)

    async def set_user_secret(self, key, value, **kwargs):
        self._write(self._path("secrets", key, kwargs), value)

    async def get_secret(self, key, **kwargs):
        return self._read(self._path("secrets", key.removeprefix("u:"), kwargs))

    async def delete_user_secret(self, key, **kwargs):
        self._path("secrets", key, kwargs).unlink(missing_ok=True)

    def clear_secret_cache(self, **kwargs):
        pass


class _Grants:
    async def set_card_credentials_expiry(self, *args, **kwargs):
        return "applied"


async def _compose(root: pathlib.Path, pool, *, tenant: str):
    """The production composition over the durable state under ``root``: what every process builds."""
    from contextlib import asynccontextmanager

    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore
    from test_card_service import _Cache, _authority
    from test_w578_account_incarnation import _AccountLocks
    from test_w578_disconnect_group import _Persistence

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield  # one process at a time in this test; a SIGKILL releases a real flock anyway

    store = BundleStorageDelegatedCardStore(root / "cards")
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    grantor = _authority().grantor_subject
    accounts = DelegatedToKdcubeStore(user_id=grantor, backend=_FileUserConfiguration(root / "props"),
                                      account_lock=_AccountLocks())
    decisions = await composition.postgres_decision_store(pool, tenant=tenant, project="w578-kill")
    host = object.__new__(AutomationAccessService)
    host._persistence = _Persistence(store, service)
    host._caller_writers = None
    coordinator = composition.bind_card_transactions(
        host, persistence=host._persistence, decisions=decisions, grant_store=_Grants(), policies=None,
        accounts_for=lambda subject: accounts if subject == grantor else None)
    bound, intents, decisions_, _ = host._card_coordinator
    host._card_coordinator = (bound, intents, decisions_, INTENT_TTL)  # recovery presumes ABORT soon after
    return host, store, service, accounts, decisions, coordinator, grantor


async def _setup(root: pathlib.Path, pool, *, tenant: str):
    """The durable starting state: one Card binding BOUND; BOUND and FREE connected, each with a credential."""
    from connection_hub.delegated_to_kdcube.models import ConnectedAccount
    from test_card_service import NOW, SUBJECT_HASH, _authority

    host, store, service, accounts, *_ = await _compose(root, pool, tenant=tenant)
    card = _authority()
    await service.commit(card, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    binding = replace(card, card_revision=card.card_revision + 1,
                      account_scope={PROVIDER: {BOUND: ("mail.read",)}})
    await service.commit(binding, subject_hash=SUBJECT_HASH, expected_revision=card.card_revision, now=NOW)
    connected = {}
    for account_id in (BOUND, FREE):
        stored = await accounts.upsert_account(ConnectedAccount(account_id=account_id, provider_id=PROVIDER,
                                                                claims=("mail.read",)))
        await accounts.set_credential(stored.credential_id, {"access_token_ref": f"{account_id}-credential"})
        connected[account_id] = stored
    return binding, connected


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def _child_disconnect(root: pathlib.Path, dsn: str, tenant: str, account_id: str, cut: str) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    host, store, service, accounts, decisions, coordinator, grantor = await _compose(root, pool, tenant=tenant)

    def dying(target, name, *, when=lambda *a, **k: True):
        real = getattr(target, name)

        async def replacement(*args, **kwargs):
            if when(*args, **kwargs):
                _die()
            return await real(*args, **kwargs)

        setattr(target, name, replacement)

    if cut == "before_stage":  # fenced, intent recorded, nothing prepared
        dying(coordinator, "prepare_existing")
    elif cut == "after_stage":  # prepared (incarnation held), no decision
        dying(coordinator, "decide")
    elif cut == "after_commit_before_delete":
        dying(accounts, "disconnect_incarnation")
    elif cut == "during_delete":  # the record is gone and the pin says deleting; the credential is not
        dying(accounts, "delete_credential")
    elif cut == "after_delete_before_finish":  # deleted and pinned; the fence not yet released
        dying(service, "release_accounts")
    else:
        raise SystemExit(f"unknown cut {cut}")
    await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                 account_id=account_id)
    raise SystemExit(3)  # the cut was never reached


async def _child_recover(root: pathlib.Path, dsn: str, tenant: str) -> None:
    import asyncpg

    from connection_hub.delegated_credentials.cards.composition import recover_card_transactions

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    *_, coordinator, _ = await _compose(root, pool, tenant=tenant)
    await asyncio.sleep(INTENT_TTL + 0.5)  # past the intent, so an undecided transaction is presumed aborted
    result = await recover_card_transactions(coordinator, limit=10)
    print(json.dumps({"recovered": result}, default=str))


def _run_child(*args: str) -> subprocess.CompletedProcess:
    here = pathlib.Path(__file__).resolve().parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(here), os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), *args], env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
async def database():
    import asyncpg

    from connection_hub.delegated_credentials.cards import composition

    dsn = os.environ[DSN_ENV]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tenant = f"t{uuid.uuid4().hex[:10]}"
    try:
        yield dsn, pool, tenant
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                composition.decision_namespace(tenant=tenant, project="w578-kill"))
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [BOUND, FREE], ids=["card-group", "effects-only"])
@pytest.mark.parametrize("cut", CUTS)
async def test_a_disconnect_killed_at_the_cut_is_recovered_by_a_fresh_process(tmp_path, database, account_id, cut):
    from connection_hub.delegated_credentials.cards import account_fence as fence
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from test_card_service import SUBJECT_HASH

    dsn, pool, tenant = database
    binding, connected = await _setup(tmp_path, pool, tenant=tenant)

    killed = _run_child("disconnect", str(tmp_path), dsn, tenant, account_id, cut)
    assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stdout, killed.stderr)
    recovered = _run_child("recover", str(tmp_path), dsn, tenant)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)

    _, store, _, accounts, decisions, _, _ = await _compose(tmp_path, pool, tenant=tenant)
    rows = await decisions.list_in_doubt(limit=10)
    assert rows == [], rows
    assert await tx.list_in_doubt(store) == []
    assert not (fence._dir(store, PROVIDER, account_id) / "fence.json").exists()
    assert await accounts._prop(accounts.account_delete_hold_key(account_id)) is None
    card = (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=binding.access_id))[1]
    stored = await accounts.get_account(account_id)
    if cut in UNDECIDED_CUTS:
        # Presumed ABORT: nothing of the disconnect happened.
        assert stored is not None and stored.incarnation == connected[account_id].incarnation
        assert await accounts.get_credential(connected[account_id].credential_id)
        assert card == binding
    else:
        # COMMIT stands and recovery finished it: exactly this incarnation is gone, with its cleanup.
        assert stored is None and account_id not in await accounts._index()
        assert not await accounts.get_credential(connected[account_id].credential_id)
        if account_id == BOUND:
            assert BOUND not in dict(card.account_scope.get(PROVIDER) or {})
            assert card.card_revision == binding.card_revision + 1
        else:
            assert card == binding  # the effects-only path writes no Card
    # The other account is never touched.
    other = FREE if account_id == BOUND else BOUND
    assert (await accounts.get_account(other)).incarnation == connected[other].incarnation


if __name__ == "__main__":
    mode, root, dsn, tenant, *rest = sys.argv[1:]
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    if mode == "disconnect":
        asyncio.run(_child_disconnect(pathlib.Path(root), dsn, tenant, *rest))
    elif mode == "recover":
        asyncio.run(_child_recover(pathlib.Path(root), dsn, tenant))
    else:
        raise SystemExit(f"unknown mode {mode}")

"""W606: a Card edit killed between COMMIT and its handle_binding, recovered by a fresh process.

Three processes share only durable state: the Card store on disk, the
PostgreSQL decision store, the PostgreSQL handle-metadata store and (for the
agent Card) a file-backed resident secret store standing in for the host's
custody. A child commits a coordinated edit and is SIGKILLed when the effect
would move the handle row; a fresh child runs ``recover_card_transactions``.
The committed Card is then readable with the SAME credential: the connector
row moved in place, the agent's same bearer re-wrapped under a fresh ref.
Synthetic: the Cards, the bearer and the file secret store.

Skipped unless ``CONNECTION_HUB_TEST_POSTGRES_DSN_FILE`` (preferred) or
``CONNECTION_HUB_TEST_POSTGRES_DSN`` names a disposable database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

DSN_ENV = "CONNECTION_HUB_TEST_POSTGRES_DSN"
DSN_FILE_ENV = "CONNECTION_HUB_TEST_POSTGRES_DSN_FILE"
BEARER = "kst1.w606-synthetic-agent-bearer-0123456789abcdef"


def _dsn() -> str:
    path = os.environ.get(DSN_FILE_ENV, "")
    return pathlib.Path(path).read_text().strip() if path else os.environ.get(DSN_ENV, "")


pytestmark = pytest.mark.skipif(not (os.environ.get(DSN_FILE_ENV) or os.environ.get(DSN_ENV)),
                                reason=f"needs a disposable PostgreSQL ({DSN_FILE_ENV} or {DSN_ENV})")


class _FileSecretStore:
    """A durable resident-secret store for separate processes: one file per ref, written atomically."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, secret_ref: str) -> pathlib.Path:
        return self.root / hashlib.sha256(secret_ref.encode("utf-8")).hexdigest()

    async def create(self, *, secret_ref: str, value: str, expires_at: int) -> bool:
        path = self._path(secret_ref)
        if path.exists():
            return False
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(value)
        os.replace(temporary, path)
        return True

    async def get(self, *, secret_ref: str) -> str | None:
        path = self._path(secret_ref)
        return path.read_text() if path.exists() else None

    async def delete(self, *, secret_ref: str) -> None:
        self._path(secret_ref).unlink(missing_ok=True)

    async def purge_expired(self, *, now: int, limit: int) -> int:
        return 0


async def _compose(root: pathlib.Path, pool, *, tenant: str):
    """The production binding over the durable state under ``root``: what every process builds."""
    from contextlib import asynccontextmanager

    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.cards.credential_handles import PostgresCardCredentialHandleStore
    from connection_hub.delegated_credentials.cards.handle_authority import PostgresCardHandleMetadataStore
    from connection_hub.delegated_credentials.cards.resident_secrets.service import ResidentCardSecretService
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from test_card_service import _Cache
    from test_w606_coordinated_write_handle_binding import _NoGrants, _Persistence

    @asynccontextmanager
    async def mutation_lock(**_kwargs):
        yield  # one process at a time in this test

    metadata = PostgresCardHandleMetadataStore(pg_pool=pool, tenant=tenant, project="w606-kill")
    await metadata.ensure_schema()
    resident = ResidentCardSecretService(metadata_store=metadata, secret_store=_FileSecretStore(root / "secrets"))
    handles = PostgresCardCredentialHandleStore(metadata_store=metadata, resident_secrets=resident)
    store = BundleStorageDelegatedCardStore(root / "cards")
    cards = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    persistence = _Persistence(store, cards, handles)
    decisions = await composition.postgres_decision_store(pool, tenant=tenant, project="w606-kill")
    host = object.__new__(AutomationAccessService)
    host._persistence = persistence
    host._caller_writers = None
    coordinator = composition.bind_card_transactions(
        host, persistence=SimpleNamespace(card_store=store, card_service=cards), decisions=decisions,
        grant_store=_NoGrants(), policies=None, credential_handles=handles)
    return SimpleNamespace(host=host, store=store, cards=cards, handles=handles, metadata=metadata,
                           resident=resident, persistence=persistence, coordinator=coordinator)


def _card(kind: str):
    from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT, CARD_KIND_CONNECTOR
    from test_caller_writer_gate import _card as base

    card = base(bound=False, revision=1)
    return dataclasses.replace(card, access_id=f"w606-{kind}", source="manual" if kind == "agent" else "oauth",
                               card_kind=CARD_KIND_AGENT if kind == "agent" else CARD_KIND_CONNECTOR,
                               created_at=1_800_000_000 - 60, expires_at=int(time.time()) + 3600)


def _held(card):
    from connection_hub.delegated_credentials.cards.model import CardCredentialHandles

    bearer = BEARER if card.card_kind == "agent" else ""
    return CardCredentialHandles(access_id=card.access_id, access_token=bearer, session_id="session-1")


async def _open():
    import asyncpg

    return await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)


async def _child_setup(root: pathlib.Path, tenant: str, kind: str) -> None:
    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    w = await _compose(root, await _open(), tenant=tenant)
    card = _card(kind)
    await w.cards.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0)
    await w.handles.write(card, _held(card))


async def _child_edit(root: pathlib.Path, tenant: str, kind: str) -> None:
    from connection_hub.delegated_credentials.automation_access import record_from_card

    w = await _compose(root, await _open(), tenant=tenant)

    async def die(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGKILL)

    w.handles.advance_binding = die  # a connector row moves at COMMIT
    w.handles.commit_rewrap = die    # an agent row's prepared envelope is installed at COMMIT
    card = _card(kind)
    edited = dataclasses.replace(card, card_revision=2, label="edited", expires_at=card.expires_at + 600)
    await w.host._persist_record(record_from_card(edited, _held(card)), expected_revision=1)
    raise SystemExit(3)  # the cut was never reached


async def _child_recover(root: pathlib.Path, tenant: str) -> None:
    from connection_hub.delegated_credentials.cards.composition import recover_card_transactions

    w = await _compose(root, await _open(), tenant=tenant)
    print(json.dumps({"recovered": await recover_card_transactions(w.coordinator, limit=10)}, default=str))


def _run_child(*args: str) -> subprocess.CompletedProcess:
    here = pathlib.Path(__file__).resolve().parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(here), os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), *args], env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
async def database():
    from connection_hub.delegated_credentials.cards import composition
    from connection_hub.delegated_credentials.cards.handle_schema import card_handle_schema

    pool = await _open()
    tenant = f"t{uuid.uuid4().hex[:10]}"
    try:
        yield pool, tenant
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                composition.decision_namespace(tenant=tenant, project="w606-kill"))
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {card_handle_schema(tenant=tenant, project='w606-kill')} CASCADE")
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connector", "agent"])
async def test_an_edit_killed_before_its_handle_binding_is_recovered_with_the_same_credential(
        tmp_path, database, kind):
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    pool, tenant = database
    setup = _run_child("setup", str(tmp_path), tenant, kind)
    assert setup.returncode == 0, (setup.stdout, setup.stderr)
    killed = _run_child("edit", str(tmp_path), tenant, kind)
    assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stdout, killed.stderr)
    recovered = _run_child("recover", str(tmp_path), tenant)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)

    w = await _compose(tmp_path, pool, tenant=tenant)
    card = _card(kind)
    subject_hash = subject_hash_for(card.grantor_subject)
    committed = (await w.store.read_current_authority(subject_hash=subject_hash, access_id=card.access_id))[1]
    assert committed.card_revision == 2  # the COMMIT stood
    row = await w.metadata.read_current(card.access_id)
    assert (row.card_revision, row.expires_at, row.session_id) == (2, committed.expires_at, "session-1")
    loaded = await w.persistence.load(card.access_id, subject_hash=subject_hash)
    assert loaded[0] == committed
    if kind == "agent":
        assert loaded[1].access_token == BEARER  # the same bearer, re-wrapped once under a fresh ref
    assert await tx.list_in_doubt(w.store) == []


if __name__ == "__main__":
    mode, root, tenant, *rest = sys.argv[1:]
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    runner = {"setup": _child_setup, "edit": _child_edit}.get(mode)
    if runner is not None:
        asyncio.run(runner(pathlib.Path(root), tenant, *rest))
    elif mode == "recover":
        asyncio.run(_child_recover(pathlib.Path(root), tenant))
    else:
        raise SystemExit(f"unknown mode {mode}")

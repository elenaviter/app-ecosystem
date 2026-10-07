"""W578: the account fence against a binding writer in ANOTHER OS process, with the production lock.

Both sides use the SDK's ``DelegatedCardService``, whose mutation lock is
KDCube's ``observed_file_lock_async`` (``fcntl.flock`` on the Card's lock file
in the shared store), over one store root declared ``same-host-flock``. The
child writer adds a connected account to a Card and stops at a chosen point:
inside its section before the durable commit, killed there, killed after the
commit before its mark is removed, or with a mark removal that fails. The
parent is the disconnect (``reserve_accounts``) and reads only durable state.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.cards.transaction_store import CardTransactionRefused
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import (
    DelegatedCardService,
)

SUBJECT = "platform-user-1"
ACCOUNT = ("google", "acct-1")
DISCONNECT = "d" * 64

_CHILD = r'''
import asyncio, dataclasses, os, signal, sys
from connection_hub.delegated_credentials.cards import account_fence
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import (
    DelegatedCardService,
)

class Cache:
    async def claim_transition(self, *a, **k): return True
    async def reconcile_projection(self, *a, **k): return False
    async def commit_projection(self, *a, **k): return True
    async def commit_tombstone(self, *a, **k): return True
    async def index_add(self, **k): return None
    async def index_remove(self, **k): return None
    async def finalize_removal(self, *a, **k): return None
    async def read(self, *a, **k): return None

async def main():
    root, mode, access_id = sys.argv[1], sys.argv[2], sys.argv[3]
    store = BundleStorageDelegatedCardStore(root, lifecycle_lock_scope="same-host-flock")
    service = DelegatedCardService(store=store, cache=Cache())
    subject_hash = subject_hash_for("platform-user-1")
    current = (await store.read_current_authority(subject_hash=subject_hash, access_id=access_id))[1]
    candidate = dataclasses.replace(current, card_revision=current.card_revision + 1,
                                    account_scope={"google": {"acct-1": ("mail.read",)}})
    real_commit_durable = service._commit_durable
    real_clear = account_fence.clear_binding

    async def commit_durable(**kwargs):
        if mode in ("hold", "kill-before-commit"):
            print("CHILD_IN_SECTION_MARKED", flush=True)
            if mode == "kill-before-commit":
                await asyncio.to_thread(sys.stdin.readline)
                os.kill(os.getpid(), signal.SIGKILL)
            if await asyncio.to_thread(sys.stdin.readline) != "continue\n":
                raise RuntimeError("release missing")
        return await real_commit_durable(**kwargs)

    def clear(store_, accounts, *, mark_id):
        if mode == "kill-after-commit":
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "removal-fails":
            return  # the mark file stays behind although the writer finished
        return real_clear(store_, accounts, mark_id=mark_id)

    service._commit_durable = commit_durable
    account_fence.clear_binding = clear
    try:
        await service.commit(candidate, subject_hash=subject_hash, expected_revision=current.card_revision)
    except Exception as exc:
        print("CHILD_REFUSED", getattr(exc, "reason", type(exc).__name__), flush=True)
        return
    print("CHILD_COMMITTED", flush=True)

asyncio.run(main())
'''


class _Cache:
    async def claim_transition(self, *a, **k): return True
    async def reconcile_projection(self, *a, **k): return False
    async def commit_projection(self, *a, **k): return True
    async def commit_tombstone(self, *a, **k): return True
    async def index_add(self, **k): return None
    async def index_remove(self, **k): return None
    async def finalize_removal(self, *a, **k): return None
    async def read(self, *a, **k): return None


class _Decisions:
    async def decision(self, receipt):
        return "undecided"


async def _world(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    tx.bind_transaction_decisions(store, _Decisions())
    service = DelegatedCardService(store=store, cache=_Cache())
    card = CardAuthority(access_id="agent-card-1", grantor_subject=SUBJECT, client_id="automation:abc",
                         delegate_subject="integration:automation:abc", source="manual", card_kind="automation",
                         card_revision=1, state="active", named_service_operations=NamedServiceSelection.none())
    from connection_hub.delegated_credentials.cards.store import subject_hash_for
    subject_hash = subject_hash_for(SUBJECT)
    await service.commit(card, subject_hash=subject_hash, expected_revision=0)
    return store, service, card, subject_hash


def _spawn(tmp_path, mode, card):
    env = dict(os.environ)
    return subprocess.Popen([sys.executable, "-c", _CHILD, str(tmp_path), mode, card.access_id],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env)


async def _line(process):
    return (await asyncio.to_thread(process.stdout.readline)).strip()


async def _bound(store, card, subject_hash):
    current = await store.read_current_authority(subject_hash=subject_hash, access_id=card.access_id)
    return fence.bound_accounts(current[1])


def _marks(store):
    return list((fence._dir(store, *ACCOUNT) / "pending").glob("*.json"))


@pytest.mark.asyncio
async def test_a_writer_holding_its_section_in_another_process_blocks_the_disconnect(tmp_path):
    store, service, card, subject_hash = await _world(tmp_path)
    child = _spawn(tmp_path, "hold", card)
    try:
        assert await _line(child) == "CHILD_IN_SECTION_MARKED"
        with pytest.raises(CardTransactionRefused, match="card_account_binding_in_progress"):
            await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
        child.stdin.write("continue\n")
        child.stdin.flush()
        assert await _line(child) == "CHILD_COMMITTED"
    finally:
        child.wait(timeout=20)
    # Its binding is committed and its mark gone: the disconnect now proceeds and will list this Card.
    assert _marks(store) == [] and ACCOUNT in await _bound(store, card, subject_hash)
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)


@pytest.mark.asyncio
async def test_a_writer_killed_before_its_commit_leaves_no_binding_and_its_mark_is_cleared(tmp_path):
    store, service, card, subject_hash = await _world(tmp_path)
    child = _spawn(tmp_path, "kill-before-commit", card)
    assert await _line(child) == "CHILD_IN_SECTION_MARKED"
    child.stdin.write("go\n")
    child.stdin.flush()
    assert child.wait(timeout=20) == -signal.SIGKILL
    assert len(_marks(store)) == 1  # the dead writer's mark is still there
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)  # the free section proves it dead
    assert _marks(store) == [] and ACCOUNT not in await _bound(store, card, subject_hash)


@pytest.mark.asyncio
async def test_a_writer_killed_after_its_commit_leaves_a_listed_binding_and_its_mark_is_cleared(tmp_path):
    store, service, card, subject_hash = await _world(tmp_path)
    child = _spawn(tmp_path, "kill-after-commit", card)
    assert child.wait(timeout=20) == -signal.SIGKILL
    assert len(_marks(store)) == 1
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    # The binding landed before the fence, so the disconnect's listing includes it: nothing is hidden.
    assert _marks(store) == [] and ACCOUNT in await _bound(store, card, subject_hash)


@pytest.mark.asyncio
async def test_a_mark_whose_removal_failed_is_cleared_after_its_live_writer_finished(tmp_path):
    store, service, card, subject_hash = await _world(tmp_path)
    child = _spawn(tmp_path, "removal-fails", card)
    assert await _line(child) == "CHILD_COMMITTED"
    child.wait(timeout=20)
    assert len(_marks(store)) == 1
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    assert _marks(store) == [] and ACCOUNT in await _bound(store, card, subject_hash)


@pytest.mark.asyncio
async def test_a_fence_written_by_another_process_refuses_the_writer(tmp_path):
    store, service, card, subject_hash = await _world(tmp_path)
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    child = _spawn(tmp_path, "commit", card)
    assert await _line(child) == "CHILD_REFUSED card_account_reserved"
    child.wait(timeout=20)
    assert _marks(store) == [] and ACCOUNT not in await _bound(store, card, subject_hash)

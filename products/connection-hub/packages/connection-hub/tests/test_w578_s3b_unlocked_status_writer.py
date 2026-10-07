"""W578 S3b review: an account writer that does not take the shared account lock.

S3b binds the lock only to the stores the Connection Hub app builds. The SDK
client (``kdcube_ai_app...connection_hub.delegated_to_kdcube.client``), which
other bundles' processes use through ``from_connection_hub``, builds its own
``DelegatedToKdcubeStore`` subclass with no ``account_lock``; its broker
writes ``set_account_status`` (and refreshed credentials) by read-then-write.
A status write that read the record before the committed disconnect deleted
it writes the record back: the disconnected account reappears with the old
incarnation.

The interleaving is forced by pausing the unlocked writer after its read;
both stores share one backend, as all processes share the user's storage.

Expected: the deleted account stays deleted (a locked or conditional status
write refuses or no-ops on a missing record).
"""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore
from test_account_store import _MemoryUserConfiguration
from test_w578_account_incarnation import GRANTOR, _AccountLocks, _account


@pytest.mark.asyncio
async def test_a_status_write_from_an_unlocked_process_cannot_resurrect_a_disconnected_account():
    backend = _MemoryUserConfiguration()
    hub = DelegatedToKdcubeStore(user_id=GRANTOR, backend=backend, account_lock=_AccountLocks())
    sdk = DelegatedToKdcubeStore(user_id=GRANTOR, backend=backend)  # as the SDK client builds it today
    first = await hub.upsert_account(_account())
    await hub.hold_incarnation_for_delete("account-1", first.incarnation, pin="e" * 64)

    read_done, deletion_done = asyncio.Event(), asyncio.Event()
    real_get = sdk.get_account

    async def get_then_pause(account_id):
        found = await real_get(account_id)
        read_done.set()
        await deletion_done.wait()
        return found

    sdk.get_account = get_then_pause
    writer = asyncio.create_task(sdk.set_account_status("account-1", "reconnect_required"))
    await read_done.wait()
    assert await hub.disconnect_incarnation("account-1", first.incarnation, pin="e" * 64) == "disconnected"
    deletion_done.set()
    await writer

    assert await hub.get_account("account-1") is None, "a disconnected account reappeared after an unlocked status write"

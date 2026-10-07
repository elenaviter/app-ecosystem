"""W578: a reconnect while a staged disconnect holds the account is answered "try again", changing nothing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from test_delegated_password_credentials import _config, _MemoryBackend

from connection_hub.delegated_to_kdcube.operations import DelegatedToKdcubeOperations
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore

FORM = {"provider_id": "icloud_mail", "connector_app_id": "app_password", "email": "person@icloud.example",
        "claims": ["email:read"], "app_password": "abcd-efgh-ijkl-mnop"}


def _locks():
    held = {}

    @asynccontextmanager
    async def lock(user_id, account_id):
        guard = held.setdefault((user_id, account_id), asyncio.Lock())
        async with guard:
            yield

    return lock


def test_a_reconnect_during_a_held_disconnect_is_a_retryable_answer_and_changes_nothing():
    async def run():
        store = DelegatedToKdcubeStore(user_id="user-1", backend=_MemoryBackend(), account_lock=_locks())
        ops = DelegatedToKdcubeOperations(config=_config(), store=store)
        await ops.connect_credential(FORM)
        [account] = await store.list_accounts(provider_id="icloud_mail")
        await store.hold_incarnation_for_delete(account.account_id, account.incarnation, pin="e" * 64)
        result = await ops.connect_credential({**FORM, "app_password": "new-pass-word-here"})
        assert result == {"ok": False, "error": "account_disconnect_pending", "retryable": True, "status": 409,
                          "message": "This account is being disconnected. Try connecting it again in a moment."}
        assert (await store.get_account(account.account_id)).incarnation == account.incarnation
        assert (await store.get_credential(account.credential_id))["app_password"] == "abcd-efgh-ijkl-mnop"

    asyncio.run(run())

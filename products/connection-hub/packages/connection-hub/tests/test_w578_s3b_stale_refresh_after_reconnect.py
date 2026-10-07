"""W578 S3b review: a refresh of the old connection that finishes after a disconnect and reconnect.

``set_account_credential`` writes only while the stored account still owns the
credential id. The credential id is deterministic per account, so a
reconnection (a new incarnation) owns the same id. A refresh that read the
old connection's credential before the disconnect, and finishes after the
person reconnected, therefore passes the check and overwrites the fresh
consent's credential with the old connection's refreshed token.

Expected: the reconnection's credential survives; the stale refresh writes
nothing (bind the incarnation it refreshed, not the deterministic id).
"""

from __future__ import annotations

import pytest

from test_w578_account_incarnation import _account, _store


@pytest.mark.asyncio
async def test_a_stale_refresh_never_overwrites_a_reconnections_credential():
    store = _store()
    first = await store.upsert_account(_account())
    await store.set_credential(first.credential_id, {"access_token_ref": "old-connection"})

    # A refresh of the OLD connection reads what it needs, then the provider call takes a while...
    refreshing_account, refreshing_credential = first.account_id, first.credential_id

    # ...meanwhile the person disconnects and reconnects (fresh consent).
    assert await store.disconnect_account(first.account_id)
    second = await store.upsert_account(_account(display_name="reconnected"))
    assert second.incarnation != first.incarnation and second.credential_id == first.credential_id
    await store.set_credential(second.credential_id, {"access_token_ref": "fresh-consent"})

    # The old refresh completes.
    await store.set_account_credential(refreshing_account, refreshing_credential,
                                       {"access_token_ref": "old-connection-refreshed"})

    assert (await store.get_credential(second.credential_id)).get("access_token_ref") == "fresh-consent", \
        "a stale refresh overwrote the reconnection's credential"

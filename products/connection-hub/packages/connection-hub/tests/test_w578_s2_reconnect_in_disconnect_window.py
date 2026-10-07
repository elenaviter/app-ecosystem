"""W578 S2 review: a reconnection between a disconnect's prepare and its effect.

The disconnect binds the stored incarnation at prepare (``ensure_incarnation``)
and deletes it after COMMIT (``disconnect_incarnation``). A reconnect in
between goes through ``upsert_account``, which keeps a stored incarnation, and
then ``set_credential`` under the deterministic credential id. The delayed
effect then still matches and deletes the person's fresh connection and its
new credential, although ``ConnectedAccount.incarnation`` promises that "a
delayed cleanup of one connection never removes a later reconnection".

Expected: the later connection survives, either because the reconnect is
refused while that incarnation's deletion is pending, or because it is
stored as a new incarnation.
"""

from __future__ import annotations

import pytest

from test_w578_account_incarnation import GRANTOR, _account, _store


@pytest.mark.asyncio
async def test_a_reconnect_after_the_disconnect_bound_its_incarnation_survives_the_delayed_effect():
    store = _store()
    first = await store.upsert_account(_account())
    bound = await store.ensure_incarnation("account-1")  # the disconnect's plan binds the incarnation
    assert bound == first.incarnation
    # claude-app: the effect's STAGE (prepare_once) holds it; a crash before STAGE leaves no hold.
    await store.hold_incarnation_for_delete("account-1", bound, pin="e" * 64)

    # The person reconnects (operations.py: upsert_account, then set_credential).
    refused = None
    try:
        reconnected = await store.upsert_account(_account(display_name="reconnected"))
        await store.set_credential(reconnected.credential_id, {"access_token_ref": "synthetic-new"})
    except Exception as exc:  # a refusal while the deletion is pending is an acceptable design
        refused = exc

    await store.disconnect_incarnation("account-1", bound, pin="e" * 64)  # the COMMITted effect

    if refused is None:
        survivor = await store.get_account("account-1")
        assert survivor is not None, "the delayed effect removed the later reconnection"
        assert await store.get_credential(survivor.credential_id), \
            "the delayed effect removed the reconnection's new credential"

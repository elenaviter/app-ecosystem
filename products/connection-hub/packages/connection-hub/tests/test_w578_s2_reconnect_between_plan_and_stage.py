"""W578 S2 review: a reconnect between the disconnect's PLAN binding and its STAGE hold.

The plan binds the stored incarnation (``ensure_incarnation``); the effect's
STAGE later holds it (``hold_incarnation_for_delete``). A reconnect in between
runs ``upsert_account``, which keeps a stored incarnation, and writes a new
credential under the deterministic credential id. The STAGE check then still
sees the bound incarnation, holds it, and the committed effect deletes the
person's fresh connection: the window of the first finding moved, it did not
close.

Expected: STAGE refuses (the connection the plan bound is no longer the stored
one), or the reconnect is refused while a plan binds the incarnation, or the
later connection otherwise survives.
"""

from __future__ import annotations

import pytest

from test_w578_account_incarnation import _account, _store


@pytest.mark.asyncio
async def test_a_reconnect_after_the_plan_bound_the_incarnation_is_never_deleted():
    store = _store()
    await store.upsert_account(_account())
    bound = await store.ensure_incarnation("account-1")  # the disconnect's PLAN

    refused = None
    try:  # the person reconnects (operations.py: upsert_account, then set_credential)
        reconnected = await store.upsert_account(_account(display_name="reconnected"))
        await store.set_credential(reconnected.credential_id, {"access_token_ref": "synthetic-new"})
    except Exception as exc:  # refusing the reconnect while the plan binds the incarnation is acceptable
        refused = exc
    if refused is not None:
        return

    try:
        await store.hold_incarnation_for_delete("account-1", bound, pin="e" * 64)  # the effect's STAGE
    except Exception:
        return  # STAGE refusing a moved connection is acceptable: nothing is deleted
    await store.disconnect_incarnation("account-1", bound, pin="e" * 64)  # the COMMITted effect

    survivor = await store.get_account("account-1")
    assert survivor is not None, "the committed effect removed a reconnection made after the plan"
    assert await store.get_credential(survivor.credential_id), "the reconnection's credential was removed"

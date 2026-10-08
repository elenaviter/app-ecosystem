"""W578 S3c review: STAGE of an effects-only disconnect refuses when its fence is not held.

``stage_effects_transaction`` checks, inside the account sections, that every
account's fence is this transaction's ("so COMMIT can only ever apply an
effect whose account no new binding can reach"). No test pinned it: removing
the check left the whole S3c suite green. Here the transaction begins and
fences, its fence is then released (the initiator's prepare stalled; a
recovery pass released it), and a late STAGE must refuse rather than hold the
incarnation for a COMMIT that no fence protects.
"""

from __future__ import annotations

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused

from test_w578_disconnect_group import _transaction_id
from test_w578_s3c_effects_only_disconnect import _disconnect, _fenced, _free_world, _held


@pytest.mark.asyncio
async def test_a_stage_whose_fence_was_released_refuses_and_holds_nothing(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    coordinator = host._card_coordinator[0]
    real_prepare, real_decide = coordinator.prepare_existing, coordinator.decide

    async def stalls(*args, **kwargs):
        raise RuntimeError("initiator stalled")

    monkeypatch.setattr(coordinator, "prepare_existing", stalls)
    monkeypatch.setattr(coordinator, "decide", stalls)
    await _disconnect(host, grantor)
    monkeypatch.setattr(coordinator, "prepare_existing", real_prepare)
    monkeypatch.setattr(coordinator, "decide", real_decide)
    transaction_id = _transaction_id(decisions)
    assert _fenced(store)

    await host._persistence.card_service.release_accounts(transaction_id)  # the fence is gone
    assert not _fenced(store)

    with pytest.raises(DecisionRefused):
        await coordinator.participants["connection-hub.card"].prepare(transaction_id)
    assert not await _held(accounts), "an unfenced STAGE must not hold the incarnation"
    assert await accounts.get_account(connected.account_id) is not None

"""W578 S3a review: a Card read that fails AFTER the disconnect's begin and fence.

Production re-lists through ``list_active``, which resolves each Card through
the serving cache; while a committed candidate's serving is being updated it
refuses (``CardUnavailable``). That is the right answer (fail closed rather
than miss a binding), but it escapes ``disconnect_account_in_transaction``
after ``begin`` and ``reserve_accounts``: the transaction stays undecided and
its fence keeps refusing every binding and every retry of the disconnect
until recovery's presumed abort.

Expected: a finite, retryable refusal, the transaction ABORTED and the fence
released, exactly as for a changed binding set.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from test_w578_disconnect_group import ACCOUNT, PROVIDER, _world


@pytest.mark.asyncio
async def test_a_card_read_failing_after_the_fence_aborts_and_releases_it(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    real = host._account_binding_members
    calls = []

    async def members(*args):
        calls.append(1)
        if len(calls) == 2:  # the re-list after the fence meets a Card whose serving is updating
            raise CardUnavailable("serving_state_unavailable")
        return await real(*args)

    monkeypatch.setattr(host, "_account_binding_members", members)
    try:
        result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                              account_id=ACCOUNT)
    except CardUnavailable:
        result = None
    assert result is not None, "the refusal escaped after begin and the fence"
    assert result["removed"] is False and result.get("retryable") is True
    assert decisions.decisions == ["aborted"], "the begun transaction must be decided, not left to recovery"
    assert not (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists(), "the fence must be released"
    assert await accounts.get_account(ACCOUNT) is not None

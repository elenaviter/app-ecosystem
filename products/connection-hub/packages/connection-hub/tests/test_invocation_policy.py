from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from connection_hub.invocation_policy import (
    POLICY_ALWAYS,
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyConflict,
    InvocationPolicyService,
    canonical_request_digest,
)


class _Locks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def __call__(self, *, lock_path, **_kwargs):
        lock = self._locks.setdefault(str(lock_path), asyncio.Lock())
        async with lock:
            yield {}


def _service(tmp_path, locks=None):
    return InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path),
        mutation_lock=locks or _Locks(),
    )


def _authority(*, operation="search", account_id=""):
    return InvocationAuthority(
        access_id="access_demo",
        resource="urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567",
        surface=SURFACE_OUTER,
        operation=operation,
        provider_id="slack" if account_id else "",
        account_id=account_id,
    )


@pytest.mark.asyncio
async def test_absent_policy_is_reusable_and_legacy_calls_need_no_invocation_id(tmp_path):
    decision = await _service(tmp_path).begin(
        owner_subject="user-1",
        authority=_authority(),
        card_revision=3,
    )

    assert decision.allowed is True
    assert decision.dispatch is True
    assert decision.policy is None
    assert decision.invocation is None


@pytest.mark.asyncio
async def test_once_requires_an_invocation_id_and_consumes_one_new_request(tmp_path):
    service = _service(tmp_path)
    authority = _authority()
    policy = await service.set_policy(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        now=100,
    )
    missing = await service.begin(
        owner_subject="user-1",
        authority=authority,
    )
    first = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=canonical_request_digest({"query": "failed jobs"}),
        card_revision=4,
        authority_revision="connector:7",
        now=101,
    )
    second = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-2",
        request_digest=canonical_request_digest({"query": "other"}),
        now=102,
    )

    assert policy.remaining == 1
    assert missing.reason == "delegated_invocation_id_required"
    assert first.allowed is True and first.dispatch is True
    assert first.policy is not None and first.policy.remaining == 0
    assert first.invocation is not None
    assert first.invocation.card_revision == 4
    assert first.invocation.authority_revision == "connector:7"
    assert second.allowed is False
    assert second.reason == "delegated_invocation_limit_exhausted"


@pytest.mark.asyncio
async def test_completed_request_replays_result_and_changed_arguments_conflict(tmp_path):
    service = _service(tmp_path)
    authority = _authority()
    digest = canonical_request_digest({"query": "failed jobs"})
    await service.set_policy(
        owner_subject="user-1", authority=authority, mode=POLICY_ONCE, now=100
    )
    await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=digest,
        now=101,
    )
    await service.complete(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=digest,
        result={"records": [1, 2]},
        now=102,
    )

    replay = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=digest,
        now=103,
    )
    conflict = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=canonical_request_digest({"query": "changed"}),
        now=104,
    )

    assert replay.allowed is True
    assert replay.dispatch is False
    assert replay.replay is True
    assert replay.result == {"records": [1, 2]}
    assert conflict.allowed is False
    assert conflict.reason == "delegated_invocation_id_conflict"


@pytest.mark.asyncio
async def test_concurrent_once_requests_have_one_winner(tmp_path):
    locks = _Locks()
    service = _service(tmp_path, locks)
    authority = _authority()
    await service.set_policy(
        owner_subject="user-1", authority=authority, mode=POLICY_ONCE, now=100
    )

    async def attempt(index: int):
        return await service.begin(
            owner_subject="user-1",
            authority=authority,
            invocation_id=f"invoke-{index}",
            request_digest=canonical_request_digest({"index": index}),
            now=101,
        )

    decisions = await asyncio.gather(*(attempt(index) for index in range(12)))

    winners = [item for item in decisions if item.allowed and item.dispatch]
    assert len(winners) == 1
    assert all(
        item.reason == "delegated_invocation_limit_exhausted"
        for item in decisions
        if item not in winners
    )


@pytest.mark.asyncio
async def test_resetting_once_creates_a_new_policy_revision_and_permit(tmp_path):
    service = _service(tmp_path)
    authority = _authority()
    first_policy = await service.set_policy(
        owner_subject="user-1", authority=authority, mode=POLICY_ONCE, now=100
    )
    await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=canonical_request_digest({"request": 1}),
        now=101,
    )
    reset = await service.set_policy(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        expected_revision=first_policy.revision,
        now=102,
    )
    next_call = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-2",
        request_digest=canonical_request_digest({"request": 2}),
        now=103,
    )

    assert reset.revision == 2
    assert reset.remaining == 1
    assert next_call.allowed is True


@pytest.mark.asyncio
async def test_reusable_policy_tracks_request_idempotency_without_becoming_consumed(tmp_path):
    service = _service(tmp_path)
    authority = _authority()
    policy = await service.set_policy(
        owner_subject="user-1", authority=authority, mode=POLICY_ALWAYS, now=100
    )
    digest = canonical_request_digest({"query": "failed jobs"})
    begin = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=digest,
        now=101,
    )
    await service.complete(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-1",
        request_digest=digest,
        result={"ok": True},
        now=102,
    )
    second_request = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-2",
        request_digest=canonical_request_digest({"query": "another"}),
        now=103,
    )

    assert begin.allowed is True
    assert second_request.allowed is True
    current = await service.get(owner_subject="user-1", authority=authority)
    assert current == policy
    assert current is not None and current.remaining is None


@pytest.mark.asyncio
async def test_policy_survives_service_recomposition(tmp_path):
    authority = _authority(account_id="account-1")
    first = _service(tmp_path)
    await first.set_policy(
        owner_subject="user-1", authority=authority, mode=POLICY_ONCE, now=100
    )

    second = _service(tmp_path)
    policies = await second.list_for_card(
        owner_subject="user-1", access_id=authority.access_id
    )

    assert len(policies) == 1
    assert policies[0].authority == authority
    assert policies[0].mode == POLICY_ONCE


@pytest.mark.asyncio
async def test_operation_policy_applies_across_card_approved_accounts(tmp_path):
    service = _service(tmp_path)
    general = _authority()
    account_call = _authority(account_id="account-1")
    await service.set_policy(
        owner_subject="user-1",
        authority=general,
        mode=POLICY_ONCE,
        now=100,
    )

    first = await service.begin(
        owner_subject="user-1",
        authority=account_call,
        invocation_id="invoke-account-1",
        request_digest=canonical_request_digest({"channel": "C1"}),
        now=101,
    )
    second = await service.begin(
        owner_subject="user-1",
        authority=_authority(account_id="account-2"),
        invocation_id="invoke-account-2",
        request_digest=canonical_request_digest({"channel": "C2"}),
        now=102,
    )

    assert first.allowed is True
    assert first.policy is not None
    assert first.policy.authority == general
    assert first.invocation is not None
    assert first.invocation.authority == account_call
    assert second.allowed is False
    assert second.reason == "delegated_invocation_limit_exhausted"


@pytest.mark.asyncio
async def test_account_policy_overrides_operation_policy(tmp_path):
    service = _service(tmp_path)
    general = _authority()
    account_call = _authority(account_id="account-1")
    await service.set_policy(
        owner_subject="user-1",
        authority=general,
        mode=POLICY_ONCE,
        now=100,
    )
    await service.set_policy(
        owner_subject="user-1",
        authority=account_call,
        mode=POLICY_ALWAYS,
        now=101,
    )

    account_decision = await service.begin(
        owner_subject="user-1",
        authority=account_call,
        invocation_id="invoke-account-1",
        request_digest=canonical_request_digest({"channel": "C1"}),
        now=102,
    )
    other_account_decision = await service.begin(
        owner_subject="user-1",
        authority=_authority(account_id="account-2"),
        invocation_id="invoke-account-2",
        request_digest=canonical_request_digest({"channel": "C2"}),
        now=103,
    )

    assert account_decision.allowed is True
    assert account_decision.policy is not None
    assert account_decision.policy.authority == account_call
    assert account_decision.policy.mode == POLICY_ALWAYS
    assert other_account_decision.allowed is True
    assert other_account_decision.policy is not None
    assert other_account_decision.policy.authority == general
    assert other_account_decision.policy.remaining == 0


@pytest.mark.asyncio
async def test_replay_survives_later_account_policy_override(tmp_path):
    service = _service(tmp_path)
    general = _authority()
    account_call = _authority(account_id="account-1")
    digest = canonical_request_digest({"channel": "C1"})
    await service.set_policy(
        owner_subject="user-1",
        authority=general,
        mode=POLICY_ONCE,
        now=100,
    )
    await service.begin(
        owner_subject="user-1",
        authority=account_call,
        invocation_id="invoke-account-1",
        request_digest=digest,
        now=101,
    )
    await service.complete(
        owner_subject="user-1",
        authority=account_call,
        invocation_id="invoke-account-1",
        request_digest=digest,
        result={"ok": True},
        now=102,
    )
    await service.set_policy(
        owner_subject="user-1",
        authority=account_call,
        mode=POLICY_ALWAYS,
        now=103,
    )

    replay = await service.begin(
        owner_subject="user-1",
        authority=account_call,
        invocation_id="invoke-account-1",
        request_digest=digest,
        now=104,
    )

    assert replay.allowed is True
    assert replay.dispatch is False
    assert replay.replay is True
    assert replay.result == {"ok": True}


@pytest.mark.asyncio
async def test_prepared_card_policy_change_is_fail_closed_until_commit(tmp_path):
    service = _service(tmp_path)
    authority = _authority(operation="restart")
    prepared = await service.prepare_policy_change(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        change_id="grant-restart-1",
        now=100,
    )

    blocked = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-restart-1",
        request_digest=canonical_request_digest({"service": "api"}),
        now=101,
    )
    policy = await service.commit_policy_change(
        owner_subject="user-1",
        authority=authority,
        change_id="grant-restart-1",
        now=102,
    )
    committed_again = await service.commit_policy_change(
        owner_subject="user-1",
        authority=authority,
        change_id="grant-restart-1",
        now=103,
    )
    allowed = await service.begin(
        owner_subject="user-1",
        authority=authority,
        invocation_id="invoke-restart-1",
        request_digest=canonical_request_digest({"service": "api"}),
        now=104,
    )

    assert prepared.state == "prepared"
    assert blocked.allowed is False
    assert blocked.reason == "delegated_invocation_policy_changing"
    assert blocked.retryable is True
    assert policy.mode == POLICY_ONCE
    assert policy.remaining == 1
    assert committed_again == policy
    assert allowed.allowed is True
    assert allowed.policy is not None and allowed.policy.remaining == 0


@pytest.mark.asyncio
async def test_prepared_change_blocks_unrelated_policy_writer(tmp_path):
    service = _service(tmp_path)
    authority = _authority(operation="restart")
    await service.prepare_policy_change(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        change_id="grant-restart-1",
        now=100,
    )

    with pytest.raises(InvocationPolicyConflict) as raised:
        await service.set_policy(
            owner_subject="user-1",
            authority=authority,
            mode=POLICY_ALWAYS,
            now=101,
        )

    assert raised.value.reason == "invocation_policy_change_in_progress"


@pytest.mark.asyncio
async def test_general_prepared_change_blocks_account_calls_without_override(tmp_path):
    service = _service(tmp_path)
    general = _authority(operation="upload")
    account = _authority(operation="upload", account_id="account-1")
    await service.prepare_policy_change(
        owner_subject="user-1",
        authority=general,
        mode=POLICY_ONCE,
        change_id="grant-upload-1",
        now=100,
    )

    blocked = await service.begin(
        owner_subject="user-1",
        authority=account,
        invocation_id="invoke-upload-1",
        request_digest=canonical_request_digest({"name": "report.csv"}),
        now=101,
    )
    await service.set_policy(
        owner_subject="user-1",
        authority=account,
        mode=POLICY_ALWAYS,
        now=102,
    )
    overridden = await service.begin(
        owner_subject="user-1",
        authority=account,
        invocation_id="invoke-upload-1",
        request_digest=canonical_request_digest({"name": "report.csv"}),
        now=103,
    )

    assert blocked.reason == "delegated_invocation_policy_changing"
    assert overridden.allowed is True
    assert overridden.policy is not None
    assert overridden.policy.authority == account

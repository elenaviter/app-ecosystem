"""Server-owned credit policy fences native quota and durable wake recovery."""
import asyncio
import pytest

from project_board.client.codex_quota import quota_state, account_fingerprint, worker_use_credits
from test_w438_quota_redemption import _held, EMAIL, NOW
from project_board.client import wake_recovery
from project_board.contract.errors import DomainError
from test_w390_limit_refused_wake import WAKE


@pytest.mark.parametrize("setting,allowed", [(-1, -1), (0, 0), (None, None), (True, None), (1, None)])
def test_only_explicit_supported_server_values_are_accepted(setting, allowed):
    assert worker_use_credits({"worker_settings": {"use_credits": setting}}) == allowed
    assert worker_use_credits({}) is None


@pytest.mark.parametrize("use_credits,change_during_read", [(-1, False), (0, False), (None, False), (-1, True)])
def test_current_worker_consent_fences_paid_credit_wakes(tmp_path, monkeypatch, use_credits, change_during_read):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)

    def policy(value):
        field.record_worker_board_record(identity.worker_name, {
            "runtime_account": {"email": EMAIL}, "worker_settings": {"use_credits": value}})
    policy(use_credits)

    async def native(**_):
        if change_during_read:
            policy(0)
        return quota_state({"rateLimits": {"limitId": "codex", "rateLimitReachedType": "rate_limit_reached",
            "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1791496800},
            "credits": {"hasCredits": True, "balance": "12.5"}}}, observed_at=NOW,
            runtime_session_id=identity.runtime_session_id, fingerprint=account_fingerprint(EMAIL))

    supervisor._codex_quota_reader = native
    result = asyncio.run(supervisor._notify_available_input(host, channel))
    allowed = use_credits == -1 and not change_during_read
    assert len(pushes) == int(allowed)
    assert bool(result.get("wake_deferred")) is not allowed


@pytest.mark.parametrize("use_credits,wrong_account", [(-1, False), (0, False), (None, False), (-1, True)])
def test_manual_recovery_honors_the_same_saved_credit_consent(tmp_path, monkeypatch, use_credits, wrong_account):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    monkeypatch.setattr(wake_recovery, "utc_now", lambda: NOW)
    field.record_worker_board_record(identity.worker_name, {
        "runtime_account": {"email": "another@example.test" if wrong_account else EMAIL},
        "worker_settings": {"use_credits": use_credits}})
    field.record_runtime_limit_state(identity.worker_name, quota_state({"rateLimits": {
        "limitId": "codex", "rateLimitReachedType": "rate_limit_reached",
        "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1791496800},
        "credits": {"hasCredits": True, "balance": "12.5"}}}, observed_at=NOW,
        runtime_session_id=identity.runtime_session_id, fingerprint=account_fingerprint(EMAIL)))
    submitted = []
    def recover():
        return wake_recovery.recover_worker_wake(host, worker_name=identity.worker_name,
            wake_id=WAKE, requested_by="owner", notifier=lambda *args, **kwargs:
                submitted.append(kwargs) or {"delivered": True})
    if use_credits == -1 and not wrong_account:
        recover()
        assert len(submitted) == 1
    else:
        with pytest.raises(DomainError) as held:
            recover()
        assert held.value.code == ("field_worker_wake_recovery_quota_account_mismatch" if wrong_account
                                   else "field_worker_wake_recovery_held")
        assert submitted == []

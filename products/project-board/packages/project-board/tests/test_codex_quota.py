"""Native quota reads use only bounded, authenticated non-model methods."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import pytest

from project_board.client import codex_quota, limit_state as limit_module
from project_board.client.limit_state import merge_codex_quota, limit_state_line
from project_board.contract.errors import DomainError
from test_w438_quota_redemption import _capacity, EMAIL, NOW, REFUSED

SESSION = "11111111-1111-4111-8111-111111111111"
ACCOUNT = {"type": "chatgpt", "email": EMAIL}
WINDOW = {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790892000}
LIMITS = {"rateLimits": {"limitId": "codex", "primary": WINDOW,
                         "secondary": {**WINDOW, "windowDurationMins": 10080}}}


class NativeProcess:
    def __init__(self, *, before=ACCOUNT, after=ACCOUNT, hang=False, limits=LIMITS):
        self.stdin = self
        self.stdout = self
        self.returncode = None
        self.pid = 12345
        self.signals = []
        self.child_alive = True
        self.calls = []
        self.closed = False
        self.stopped = False
        self.after = after
        self.before = before
        self.hang = hang
        self.limits = limits
        self.responses = asyncio.Queue()

    def write(self, data):
        row = json.loads(data)
        self.calls.append(row)
        if self.hang or "id" not in row:
            return
        if row["method"] == "initialize":
            result = {}
        elif row["method"] == "account/read":
            result = {"account": self.before if row["id"] == 2 else self.after}
        elif row["method"] == "account/rateLimits/read":
            # Native notifications interleave with request responses.
            self.responses.put_nowait(b'{"method":"account/rateLimits/updated","params":{}}\n')
            result = self.limits
        else:
            raise AssertionError("a model, reset or credential method was sent")
        self.responses.put_nowait((json.dumps({"id": row["id"], "result": result}) + "\n").encode())

    async def drain(self):
        pass

    async def readline(self):
        return await self.responses.get()

    def close(self):
        self.closed = True

    def terminate(self):
        self.stopped = True
        self.returncode = 0

    def kill(self):
        self.terminate()

    async def wait(self):
        return self.returncode


def _native(monkeypatch, **kwargs):
    process = NativeProcess(**kwargs)
    launches = []

    async def launch(*args, **options):
        launches.append((args, options))
        return process

    monkeypatch.setattr(codex_quota.asyncio, "create_subprocess_exec", launch)

    def stop_group(pid, sig):
        assert pid == process.pid
        process.signals.append(sig)
        process.terminate()
        if sig == signal.SIGKILL:
            process.child_alive = False

    monkeypatch.setattr(codex_quota.os, "killpg", stop_group)
    return process, launches


def test_native_read_checks_account_twice_and_never_starts_a_turn(monkeypatch):
    process, launches = _native(monkeypatch)
    state = asyncio.run(codex_quota.read_codex_quota(expected_email=EMAIL,
        runtime_session_id=SESSION, executable=Path("/synthetic/codex")))
    assert state["kind"] == "ok" and state["source"] == "codex-app-server"
    assert state["account_bound"] and state["runtime_session_id"] == SESSION
    assert state["account_email_sha256"] == codex_quota.account_fingerprint(EMAIL)
    assert launches[0][0] == ("/synthetic/codex", "app-server", "--stdio")
    assert [row["method"] for row in process.calls] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read", "account/read"]
    assert [row["params"] for row in process.calls if row["method"] == "account/read"] == [
        {"refreshToken": False}, {"refreshToken": False}]
    assert process.stopped and process.closed
    assert EMAIL not in json.dumps(state), "the retained evidence contains a fingerprint, not account text"


@pytest.mark.parametrize("after", [None, {"type": "apiKey"}, {"type": "chatgpt", "email": "another@example.test"}])
def test_account_change_fails_closed_without_exposing_account_text(monkeypatch, after):
    process, _ = _native(monkeypatch, after=after)
    with pytest.raises(DomainError) as refused:
        asyncio.run(codex_quota.read_codex_quota(expected_email=EMAIL,
            runtime_session_id=SESSION, executable=Path("/synthetic/codex")))
    assert refused.value.code == "work_codex_quota_account_mismatch"
    assert "example.test" not in str(refused.value) and process.stopped


def test_timeout_is_bounded_and_stops_only_its_temporary_reader(monkeypatch):
    process, _ = _native(monkeypatch, hang=True)
    with pytest.raises(DomainError) as refused:
        asyncio.run(codex_quota.read_codex_quota(expected_email=EMAIL,
            runtime_session_id=SESSION, executable=Path("/synthetic/codex"), timeout_seconds=0.05))
    assert refused.value.code == "work_codex_quota_timeout"
    assert process.stopped and process.closed
    assert [row["method"] for row in process.calls] == ["initialize"]


def test_promptly_exiting_shim_does_not_leave_its_native_child(monkeypatch):
    process, _ = _native(monkeypatch)
    asyncio.run(codex_quota.read_codex_quota(expected_email=EMAIL,
        runtime_session_id=SESSION, executable=Path("/synthetic/codex")))
    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert not process.child_alive, "only the reader's owned process group is stopped"


def test_wrong_initial_account_never_reads_its_quota(monkeypatch):
    process, _ = _native(monkeypatch, before={"type": "chatgpt", "email": "another@example.test"})
    with pytest.raises(DomainError) as refused:
        asyncio.run(codex_quota.read_codex_quota(expected_email=EMAIL,
            runtime_session_id=SESSION, executable=Path("/synthetic/codex")))
    assert refused.value.code == "work_codex_quota_account_mismatch"
    assert [row["method"] for row in process.calls] == ["initialize", "initialized", "account/read"]
    assert process.stopped


@pytest.mark.parametrize("used", [None, True, float("nan"), float("inf"), -1])
def test_unmeasured_or_invalid_windows_are_not_positive_capacity(used):
    state = codex_quota.quota_state({"rateLimits": {"primary": {**WINDOW, "usedPercent": used}}},
        observed_at=NOW, runtime_session_id=SESSION, fingerprint="synthetic")
    assert state["kind"] == "unknown"


def test_all_buckets_are_checked_and_exhaustion_wins():
    result = {**LIMITS, "rateLimitsByLimitId": {"premium": {
        "primary": {**WINDOW, "usedPercent": 100}}}}
    state = codex_quota.quota_state(result, observed_at=NOW,
        runtime_session_id=SESSION, fingerprint="synthetic")
    assert state["kind"] == "rate_limited" and state["limit_id"] == "premium"
    assert set(state["buckets"]) == {"premium", "codex"}


@pytest.mark.parametrize("change", [{"windowDurationMins": None}, {"windowDurationMins": False},
                                      {"resetsAt": float("nan")}, {"resetsAt": -1}])
def test_incomplete_native_window_shape_is_not_positive_capacity(change):
    state = codex_quota.quota_state({"rateLimits": {"primary": {**WINDOW, **change}}},
        observed_at=NOW, runtime_session_id=SESSION, fingerprint="synthetic")
    assert state["kind"] == "unknown"


def test_native_bucket_count_is_bounded_without_silently_dropping_exhaustion():
    with pytest.raises(DomainError):
        codex_quota.quota_state({"rateLimitsByLimitId": {str(i): {"primary": WINDOW} for i in range(17)}},
            observed_at=NOW, runtime_session_id=SESSION, fingerprint="synthetic")


def _old():
    return {"kind": "rate_limited", "source": "codex-rollout", "limit_id": "codex",
        "observed_at": REFUSED, "refusal": "usage_limit_exceeded",
        "resets_at": "2999-01-01T00:00:00Z", "windows": [
            {"name": "secondary", "used_percent": 100}]}


def test_new_positive_capacity_keeps_receive_pending_not_working():
    merged = merge_codex_quota(_old(), _capacity(SESSION), runtime_session_id=SESSION, now=NOW)
    assert merged["kind"] == "ok" and merged["refused_at"] == REFUSED
    assert merged["reached"] == "capacity_available_receive_pending"
    assert limit_state_line(merged) == "capacity available; same-session receive pending"
    assert merged["observed_at"] == NOW and merged["limit_id"] == "codex"


def test_receive_after_clear_compares_instants_not_timestamp_spelling(monkeypatch):
    monkeypatch.setattr(limit_module, "codex_limit_state", lambda *_args, **_kwargs: _old())
    row = limit_module.session_with_limit_state({
        "last_inbox_result_at": "2026-10-01T00:41:00.000001Z"},
        runtime_kind="codex", runtime_session_id=SESSION,
        now="2026-10-01T00:41:01Z", recorded=_capacity(SESSION))
    assert row["limit_state"]["reached"] == "capacity_available_receive_observed"


@pytest.mark.parametrize("change", [
    {"runtime_session_id": "another"}, {"account_bound": False},
    {"observed_at": REFUSED}, {"observed_at": "2999-01-01T00:00:00Z"},
    {"buckets": {}},
])
def test_wrong_session_unbound_old_future_or_partial_read_cannot_erase_exhaustion(change):
    candidate = {**_capacity(SESSION), **change}
    assert merge_codex_quota(_old(), candidate, runtime_session_id=SESSION, now=NOW) == _old()


def test_expired_positive_read_is_unknown_not_resurrected_old_exhaustion():
    state = merge_codex_quota(_old(), _capacity(SESSION), runtime_session_id=SESSION,
                              now="2026-10-01T00:45:00Z")
    assert state["kind"] == "unknown" and state["source"] == "codex-app-server"
    assert state["observed_at"] == NOW and state["reached"] == "quota_read_stale"


def test_new_provider_refusal_wins_over_an_earlier_native_positive_read():
    old = {**_old(), "observed_at": "2026-10-01T00:42:00Z"}
    assert merge_codex_quota(old, _capacity(SESSION), runtime_session_id=SESSION,
                              now="2026-10-01T00:43:00Z") == old


def test_weekly_window_can_change_its_native_primary_secondary_label():
    old = _old()
    old["windows"][0]["window_minutes"] = 10080
    candidate = codex_quota.quota_state({"rateLimits": {"limitId": "codex", "primary": {
        **WINDOW, "windowDurationMins": 10080, "usedPercent": 46}}},
        observed_at=NOW, runtime_session_id=SESSION, fingerprint="synthetic")
    assert merge_codex_quota(old, candidate, runtime_session_id=SESSION, now=NOW)["kind"] == "ok"


def test_five_hour_capacity_does_not_cover_an_unreported_exhausted_week():
    old = _old()
    old["windows"][0]["window_minutes"] = 10080
    candidate = codex_quota.quota_state({"rateLimits": {"limitId": "codex", "primary": WINDOW}},
        observed_at=NOW, runtime_session_id=SESSION, fingerprint="synthetic")
    assert merge_codex_quota(old, candidate, runtime_session_id=SESSION, now=NOW) == old

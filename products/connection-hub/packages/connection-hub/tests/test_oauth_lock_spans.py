"""OAuth profile locks, custody and requests leave redacted, joinable evidence (W461).

On 2026-10-02 relay channels failed with ``oauth_profile_lock_timeout`` and
with token requests that never reached the proxy, and no record named a lock
holder, a wait or a request the server side could match. These tests pin the
redacted spans and the per-request id.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from connection_hub.caller.authorization import discovery, lock_spans, profile_session
from connection_hub.caller.errors import AuthorizationError

PROFILE = "problem-board-claude-0123456789ab"


def _service(tmp_path: Path):
    """The real lock methods on a minimal service: only the profile store path is used."""

    service = object.__new__(profile_session.OAuthProfileSessionService)
    service._profiles = SimpleNamespace(path=tmp_path / "profiles.json")
    service._transaction_lock = tmp_path / "profiles.json.oauth.transaction.lock"
    return service


@pytest.fixture
def short_lock_timeout(monkeypatch):
    real = profile_session.AsyncFileLock

    def quick(path, timeout=10, mode=0o600):
        return real(path, timeout=0.3, mode=mode)

    monkeypatch.setattr(profile_session, "AsyncFileLock", quick)


def _spans(caplog):
    return [r for r in caplog.records if r.name == "connection_hub.oauth.spans"]


def test_a_transaction_records_wait_and_hold_without_the_profile_name(tmp_path, caplog):
    service = _service(tmp_path)

    async def scenario():
        async with service._transaction(
            service._transaction_lock, profile_name=PROFILE, operation="read_token"
        ):
            await asyncio.sleep(0.3)

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())

    (span,) = _spans(caplog)
    text = span.getMessage()
    assert span.levelno == logging.INFO, "a hold of 0.25 s or more is logged at INFO"
    assert "kind=transaction operation=read_token" in text and "outcome=ok" in text
    assert f"profile={lock_spans.profile_tag(PROFILE)}" in text and PROFILE not in text
    hold = int(text.split("hold_ms=")[1].split()[0])
    assert hold >= 250 and "wait_ms=" in text and "pid=" in text and "task=" in text


def test_a_quick_span_stays_at_debug(tmp_path, caplog):
    service = _service(tmp_path)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            pass

    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    assert _spans(caplog) == [], "a healthy quick span adds no line at INFO"


def test_a_held_transaction_lock_times_out_with_a_warning_span(tmp_path, caplog, short_lock_timeout):
    service = _service(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with FileLock(str(service._transaction_lock)):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="commit_refreshed"):
            pass

    try:
        with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
            with pytest.raises(AuthorizationError) as raised:
                asyncio.run(scenario())
    finally:
        release.set()
        thread.join(2)
    assert raised.value.code == "oauth_profile_lock_timeout"
    (span,) = _spans(caplog)
    assert span.levelno == logging.WARNING
    assert "kind=transaction operation=commit_refreshed" in span.getMessage()
    assert "outcome=timeout" in span.getMessage() and "hold_ms=-" in span.getMessage()
    assert int(span.getMessage().split("wait_ms=")[1].split()[0]) >= 250


def test_a_refresh_slot_timeout_is_told_apart_from_the_transaction(tmp_path, caplog, short_lock_timeout):
    service = _service(tmp_path)
    slot = tmp_path / f"profiles.json.{PROFILE}.oauth.refresh.lock"
    held = threading.Event()
    release = threading.Event()

    def holder():
        with FileLock(str(slot)):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)

    async def scenario():
        async with service._refresh_slot(PROFILE):
            pass

    try:
        with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
            with pytest.raises(AuthorizationError) as raised:
                asyncio.run(scenario())
    finally:
        release.set()
        thread.join(2)
    assert "refresh lock" in str(raised.value)
    (span,) = _spans(caplog)
    assert "kind=refresh_slot" in span.getMessage() and "outcome=timeout" in span.getMessage()


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [
        (AuthorizationError("oauth_token_request_failed", "x"), "oauth_token_request_failed"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_a_failure_inside_the_lock_releases_it_and_names_the_outcome(tmp_path, caplog, raised, outcome):
    service = _service(tmp_path)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            raise raised

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        with pytest.raises(type(raised)):
            asyncio.run(scenario())
    (span,) = _spans(caplog)
    assert span.levelno == logging.WARNING and f"outcome={outcome}" in span.getMessage()

    async def again():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            return True

    assert asyncio.run(again()) is True, "the lock was released"


def test_a_waiter_cancelled_before_the_lock_leaves_a_cancelled_span(tmp_path, caplog):
    service = _service(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with FileLock(str(service._transaction_lock)):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)

    async def scenario():
        async def wait_for_lock():
            async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
                pass

        task = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0.3)
        task.cancel()
        return await asyncio.gather(task, return_exceptions=True)

    try:
        with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
            (result,) = asyncio.run(scenario())
    finally:
        release.set()
        thread.join(2)
    assert isinstance(result, asyncio.CancelledError)
    (span,) = _spans(caplog)
    assert span.levelno == logging.WARNING
    assert "outcome=cancelled" in span.getMessage() and "hold_ms=-" in span.getMessage()
    assert int(span.getMessage().split("wait_ms=")[1].split()[0]) >= 250


def test_every_oauth_request_carries_a_request_id_that_failures_report():
    httpx2 = pytest.importorskip("httpx2")
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get(discovery.REQUEST_ID_HEADER, ""))
        raise httpx2.ConnectError("no route", request=request)

    transport = discovery.HttpxOAuthTransport(transport=httpx2.MockTransport(handler), timeout_seconds=2)

    with pytest.raises(AuthorizationError) as raised:
        asyncio.run(transport.post_form("https://hub.example/oauth/token", {"grant_type": "refresh_token"}))

    request_id = raised.value.details["request_id"]
    assert seen == [request_id] and len(request_id) == 16
    assert f"request_id {request_id}" in str(raised.value)
    assert raised.value.details["failure_kind"] == "ConnectError"
    assert "refresh_token" not in str(raised.value)


def test_request_ids_are_unique_per_request():
    assert len({discovery.new_request_id() for _ in range(200)}) == 200


CANARY = "synthetic-sensitive-profile-canary"


def test_a_caller_chosen_task_name_never_reaches_the_span(tmp_path, caplog):
    service = _service(tmp_path)

    async def work():
        async with service._transaction(service._transaction_lock, profile_name=CANARY, operation="read_token"):
            raise RuntimeError("synthetic-body-error")

    async def scenario():
        task = asyncio.create_task(work(), name="refresh " + CANARY)
        with pytest.raises(RuntimeError):
            await task

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    (span,) = _spans(caplog)
    assert CANARY not in span.getMessage()
    assert re.search(r"task=t[0-9a-f]{6} corr=\S+$", span.getMessage())


def test_a_failure_securing_the_held_lock_is_the_span_outcome(tmp_path, caplog, monkeypatch):
    service = _service(tmp_path)

    def fail_secure(_path):
        raise RuntimeError("synthetic-security-step-failure")

    monkeypatch.setattr(service, "_secure_lock", fail_secure)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            pass

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        with pytest.raises(RuntimeError, match="synthetic-security-step-failure"):
            asyncio.run(scenario())
    (span,) = _spans(caplog)
    assert span.levelno == logging.WARNING and "outcome=RuntimeError" in span.getMessage()
    assert "hold_ms=-" not in span.getMessage(), "the lock was held when securing it failed"


def test_a_release_failure_is_the_span_outcome(tmp_path, caplog, monkeypatch):
    service = _service(tmp_path)

    class FailingRelease:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            raise RuntimeError("synthetic-release-step-failure")

    monkeypatch.setattr(profile_session, "AsyncFileLock", FailingRelease)
    monkeypatch.setattr(service, "_secure_lock", lambda _path: None)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            pass

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        with pytest.raises(RuntimeError, match="synthetic-release-step-failure"):
            asyncio.run(scenario())
    (span,) = _spans(caplog)
    assert span.levelno == logging.WARNING and "outcome=RuntimeError" in span.getMessage()


def test_a_long_holder_is_recorded_while_it_still_holds(tmp_path, caplog, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(lock_spans, "HOLD_WARN_SECONDS", 0.1)
    seen_while_held: list[str] = []

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="commit_refreshed"):
            await asyncio.sleep(0.3)
            seen_while_held.extend(r.getMessage() for r in _spans(caplog))

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    (holding,) = seen_while_held
    assert "outcome=holding" in holding and "kind=transaction operation=commit_refreshed" in holding
    assert int(holding.split("hold_ms=")[1].split()[0]) >= 100
    holding_task = holding.split("task=")[1]
    final = _spans(caplog)[-1].getMessage()
    assert "outcome=ok" in final and final.split("task=")[1] == holding_task
    assert len(_spans(caplog)) == 2


def test_a_quick_holder_leaves_no_holding_record(tmp_path, caplog):
    service = _service(tmp_path)

    async def scenario():
        async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
            pass
        await asyncio.sleep(0)

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    assert ["outcome=holding" in r.getMessage() for r in _spans(caplog)] == [False]

"""Every OAuth HTTP request leaves one redacted record, joinable to its spans and the proxy (W461)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.caller.authorization import discovery, lock_spans, profile_session, request_records
from connection_hub.caller.errors import AuthorizationError

httpx2 = pytest.importorskip("httpx2")

PROFILE = "problem-board-claude-0123456789ab"
CANARIES = ("CANARY-REFRESH", "CANARY-ACCESS", "secret-host.example", "CANARY-QUERY", PROFILE)
URL = "https://secret-host.example/oauth/token?state=CANARY-QUERY"


def _records(caplog):
    return [r for r in caplog.records if r.name == "connection_hub.oauth.requests"]


def _fields(record) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))


def _transport(handler):
    return discovery.HttpxOAuthTransport(transport=httpx2.MockTransport(handler), timeout_seconds=2)


def _assert_no_canary(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records)
    for canary in CANARIES:
        assert canary not in text, canary


def test_a_successful_token_request_records_its_id_status_time_and_correlation(caplog):
    sent: list[str] = []

    def handler(request):
        sent.append(request.headers[discovery.REQUEST_ID_HEADER])
        return httpx2.Response(200, json={"access_token": "CANARY-ACCESS", "token_type": "Bearer"})

    async def scenario():
        with request_records.correlate(PROFILE) as correlation:
            await _transport(handler).post_form(URL, {"grant_type": "refresh_token", "refresh_token": "CANARY-REFRESH"})
            return correlation

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.requests"):
        correlation = asyncio.run(scenario())
    (record,) = _records(caplog)
    fields = _fields(record)
    assert record.levelno == logging.DEBUG, "a quick success adds no line at INFO"
    assert fields["rid"] == sent[0] and len(sent[0]) == 16, "the id the proxy logs"
    assert fields["corr"] == correlation and fields["profile"] == lock_spans.profile_tag(PROFILE)
    assert (fields["kind"], fields["method"], fields["status"], fields["outcome"]) == ("token", "POST", "200", "ok")
    assert int(fields["elapsed_ms"]) >= 0 and fields["task"].startswith("t")
    _assert_no_canary(caplog)


def test_a_refused_token_request_is_a_warning_with_its_status(caplog):
    def handler(request):
        return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "CANARY-REFRESH"})

    async def scenario():
        with request_records.correlate(PROFILE):
            with pytest.raises(AuthorizationError):
                await _transport(handler).post_form(URL, {"refresh_token": "CANARY-REFRESH"})

    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.requests"):
        asyncio.run(scenario())
    (record,) = _records(caplog)
    fields = _fields(record)
    assert record.levelno == logging.WARNING
    assert (fields["status"], fields["outcome"]) == ("400", "oauth_token_request_failed")
    _assert_no_canary(caplog)


def test_a_connection_failure_is_recorded_without_a_status(caplog):
    def handler(request):
        raise httpx2.ConnectError("no route to secret-host.example", request=request)

    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.requests"):
        with pytest.raises(AuthorizationError):
            asyncio.run(_transport(handler).get_json(URL))
    (record,) = _records(caplog)
    fields = _fields(record)
    assert (fields["kind"], fields["status"], fields["outcome"]) == ("metadata", "-", "oauth_metadata_request_failed")
    assert fields["corr"] == "-", "outside a token operation"
    _assert_no_canary(caplog)


def test_a_cancelled_request_is_recorded_as_cancelled(caplog):
    async def handler(request):
        await asyncio.sleep(5)
        return httpx2.Response(200, json={})

    async def scenario():
        task = asyncio.create_task(_transport(handler).get_json(URL))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.requests"):
        asyncio.run(scenario())
    (record,) = _records(caplog)
    assert _fields(record)["outcome"] == "cancelled" and record.levelno == logging.WARNING


def test_the_lock_spans_and_requests_of_one_operation_share_its_correlation(tmp_path: Path, caplog):
    service = object.__new__(profile_session.OAuthProfileSessionService)
    service._profiles = SimpleNamespace(path=tmp_path / "profiles.json")
    service._transaction_lock = tmp_path / "profiles.json.oauth.transaction.lock"

    def handler(request):
        return httpx2.Response(500, json={})

    async def scenario():
        with request_records.correlate(PROFILE) as correlation:
            async with service._transaction(service._transaction_lock, profile_name=PROFILE, operation="read_token"):
                await asyncio.sleep(0.3)  # a slow hold logs at INFO
            with pytest.raises(AuthorizationError):
                await _transport(handler).get_json(URL)
            return correlation

    with caplog.at_level(logging.INFO):
        correlation = asyncio.run(scenario())
    span = [r for r in caplog.records if r.name == "connection_hub.oauth.spans"][0]
    (request,) = _records(caplog)
    assert span.getMessage().endswith(f"corr={correlation}")
    assert _fields(request)["corr"] == correlation


def test_correlations_are_distinct_and_reset():
    with request_records.correlate(PROFILE) as first:
        assert request_records.current() == (lock_spans.profile_tag(PROFILE), first)
    with request_records.correlate(PROFILE) as second:
        assert second != first
    assert request_records.current() == ("-", "-")

# SPDX-License-Identifier: MIT
import pytest

from connection_hub.browser_session.flow import (
    BrowserSessionFlow,
    LoginAttemptRejected,
    LoginRejected,
)
from connection_hub.browser_session.model import SessionPolicy

from .conftest import FakeUpstream


def _flow(backend, attempts, cookies, clock, upstream=None, policy=None):
    return BrowserSessionFlow(
        backend=backend, attempts=attempts, upstream=upstream or FakeUpstream(), cookies=cookies,
        policy=policy or SessionPolicy(idle_ttl_seconds=3600, max_ttl_seconds=10 * 3600, touch_interval_seconds=60, attempt_ttl_seconds=300),
        clock=clock,
    )


async def _sign_in(flow, next_raw="/platform/chat"):
    start = await flow.begin_login(next_raw)
    params = {"state": start.attempt.state, "code": "c", "nonce": start.attempt.nonce}
    return start, await flow.complete_login(params, attempt_binding=start.attempt_cookie.value)


async def test_login_round_trip_sets_one_httponly_cookie_and_returns_to_next(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    start = await flow.begin_login("/sites/connections/?tab=cards")
    assert start.redirect_url.startswith("https://idp.test/authorize?state=")
    assert start.attempt_cookie.name == "__Host-kdcube-login"
    assert start.attempt_cookie.http_only and start.attempt_cookie.secure and start.attempt_cookie.max_age == 300
    assert start.attempt.next_path == "/sites/connections/?tab=cards"
    assert len(attempts) == 1

    done = await flow.complete_login(
        {"state": start.attempt.state, "code": "c", "nonce": start.attempt.nonce},
        attempt_binding=start.attempt_cookie.value,
    )
    assert done.redirect_to == "/sites/connections/?tab=cards"
    assert done.session_cookie.name == "__Secure-LATC" and done.session_cookie.http_only
    assert done.session_cookie.value == done.session.token and done.session_cookie.max_age == 10 * 3600
    assert done.clear_attempt_cookie.clears and done.clear_attempt_cookie.name == "__Host-kdcube-login"
    assert done.identity.canonical_subject == "fake:sub-1"
    assert done.session.expires_at == clock.now + 3600
    assert backend.users["fake:sub-1"]["email"] == "person@example.test"
    assert len(attempts) == 0, "the attempt is consumed"


async def test_open_redirect_targets_are_replaced_at_login_start(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    _, done = await _sign_in(flow, "https://evil.test/steal")
    assert done.redirect_to == "/"


async def test_attempt_is_one_time_bound_to_the_browser_and_expires(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    start = await flow.begin_login("/")
    params = {"state": start.attempt.state, "code": "c", "nonce": start.attempt.nonce}
    with pytest.raises(LoginAttemptRejected) as other_browser:
        await flow.complete_login(params, attempt_binding="not-the-binding")
    assert other_browser.value.reason == "binding_mismatch"
    # The attempt was consumed by that failed completion: a replay finds nothing.
    with pytest.raises(LoginAttemptRejected) as replay:
        await flow.complete_login(params, attempt_binding=start.attempt_cookie.value)
    assert replay.value.reason == "attempt_missing"

    start = await flow.begin_login("/")
    clock.advance(301)
    with pytest.raises(LoginAttemptRejected) as expired:
        await flow.complete_login({"state": start.attempt.state, "code": "c", "nonce": start.attempt.nonce}, attempt_binding=start.attempt_cookie.value)
    assert expired.value.reason == "attempt_missing"

    with pytest.raises(LoginAttemptRejected):
        await flow.complete_login({"code": "c"}, attempt_binding="x")


async def test_upstream_rejection_surfaces_its_reason_and_consumes_the_attempt(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock, upstream=FakeUpstream(reject="token_invalid"))
    start = await flow.begin_login("/")
    with pytest.raises(LoginRejected) as rejected:
        await flow.complete_login({"state": start.attempt.state, "code": "c"}, attempt_binding=start.attempt_cookie.value)
    assert rejected.value.reason == "token_invalid"
    assert not isinstance(rejected.value, LoginAttemptRejected)
    assert len(attempts) == 0 and not backend.sessions


async def test_validate_slides_the_session_within_the_maximum(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    _, done = await _sign_in(flow)
    token = done.session.token
    first_expiry = done.session.expires_at

    state = await flow.validate_request(token)
    assert state is not None and state.expires_at == first_expiry, "no extension inside the touch interval"

    clock.advance(120)
    state = await flow.validate_request(token)
    assert state.expires_at == clock.now + 3600, "extended by the idle limit from now"
    assert state.last_seen_at == clock.now

    # Idle past the limit: gone.
    clock.advance(3601)
    assert await flow.validate_request(token) is None

    # A session used every hour still ends at the maximum since sign-in.
    _, done = await _sign_in(flow)
    token = done.session.token
    issued = done.session.issued_at
    state = None
    for _ in range(14):
        clock.advance(3000)
        state = await flow.validate_request(token)
        if state is None:
            break
        assert state.expires_at <= issued + 10 * 3600
    assert state is None and clock.now >= issued + 10 * 3600
    assert await flow.validate_request(token) is None
    assert await flow.validate_request(None) is None
    assert await flow.validate_request("kst1.forged.token") is None


async def test_logout_ends_the_session_clears_the_cookie_and_offers_upstream_logout(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    _, done = await _sign_in(flow)
    result = await flow.logout(done.session.token, post_logout_redirect="https://app.test/")
    assert result.ended is True
    assert result.clear_session_cookie.clears and result.clear_session_cookie.name == "__Secure-LATC"
    assert result.upstream_logout_url == "https://idp.test/logout?to=https://app.test/"
    assert await flow.validate_request(done.session.token) is None
    again = await flow.logout(done.session.token)
    assert again.ended is False


async def test_invalidating_the_user_ends_every_session(backend, attempts, cookies, clock):
    flow = _flow(backend, attempts, cookies, clock)
    _, first = await _sign_in(flow)
    _, second = await _sign_in(flow)
    assert await backend.invalidate_user("fake:sub-1") == 2
    assert await flow.validate_request(first.session.token) is None
    assert await flow.validate_request(second.session.token) is None

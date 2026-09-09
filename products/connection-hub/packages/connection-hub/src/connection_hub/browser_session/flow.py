# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""``BrowserSessionFlow``: the four moves of a server-held browser session,
as pure methods a host's router calls and translates into HTTP.

    begin_login(next)            -> where to redirect, the attempt cookie
    complete_login(params, ...)  -> the session cookie, where to redirect
    validate_request(token)      -> the live session, slid forward when due
    logout(token)                -> the cleared cookie, an upstream logout URL

The login attempt is one-time and browser-bound: ``begin_login`` stores it
under a random ``state`` and hands the browser a separate random ``binding``
in a short-lived cookie; ``complete_login`` takes the attempt out of the store
(so a code cannot be replayed) and requires the binding cookie to match, so
only the browser that started the sign-in can finish it. The destination is
validated when the login starts, never trusted from the callback.

Sliding renewal: a validated request whose last extension is older than the
touch interval moves the session's expiry to ``policy.expiry_after``, the idle
limit from now bounded by the maximum since sign-in. The cookie's max-age is
the maximum, so the browser keeps the cookie while the server decides.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from connection_hub.browser_session.model import (
    CookieSpec,
    IssuedSession,
    LoginAttempt,
    SessionPolicy,
    SessionState,
    VerifiedIdentity,
)
from connection_hub.browser_session.next_url import safe_next_path
from connection_hub.browser_session.protocols import (
    CookiePolicy,
    LoginAttemptStore,
    SessionBackend,
    UpstreamIdentity,
    UpstreamRejected,
)


class LoginRejected(Exception):
    """A sign-in that cannot complete; ``reason`` is a short code the host
    may show or log: ``attempt_missing`` (expired, consumed, or never
    started), ``binding_mismatch`` (another browser), or the upstream's own
    reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class LoginAttemptRejected(LoginRejected):
    """The attempt itself is unusable: missing, expired, or not this browser's."""


@dataclass(frozen=True)
class LoginRedirect:
    """Where to send the browser (empty when the upstream posts a credential
    to the host instead), and the attempt cookie to set."""

    redirect_url: str
    attempt_cookie: CookieSpec
    attempt: LoginAttempt


@dataclass(frozen=True)
class LoginCompleted:
    session: IssuedSession
    identity: VerifiedIdentity
    session_cookie: CookieSpec
    clear_attempt_cookie: CookieSpec
    redirect_to: str


@dataclass(frozen=True)
class LogoutCompleted:
    ended: bool
    clear_session_cookie: CookieSpec
    upstream_logout_url: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


class BrowserSessionFlow:
    def __init__(
        self,
        *,
        backend: SessionBackend,
        attempts: LoginAttemptStore,
        upstream: UpstreamIdentity,
        cookies: CookiePolicy,
        policy: SessionPolicy | None = None,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._backend = backend
        self._attempts = attempts
        self._upstream = upstream
        self._cookies = cookies
        self._policy = policy or SessionPolicy()
        self._clock = clock
        self._token = token_factory

    @property
    def policy(self) -> SessionPolicy:
        return self._policy

    @property
    def upstream(self) -> UpstreamIdentity:
        return self._upstream

    def _now(self) -> int:
        return int(self._clock())

    async def begin_login(self, next_raw: str | None = None, *, metadata: Mapping[str, Any] | None = None) -> LoginRedirect:
        now = self._now()
        attempt = LoginAttempt(
            state=self._token(32),
            binding=self._token(32),
            nonce=self._token(32),
            code_verifier=self._token(64),
            next_path=safe_next_path(next_raw),
            created_at=now,
            expires_at=now + self._policy.attempt_ttl_seconds,
            upstream=self._upstream.name,
            metadata=dict(metadata or {}),
        )
        await self._attempts.put(attempt)
        redirect_url = await self._upstream.begin(attempt)
        return LoginRedirect(
            redirect_url=redirect_url,
            attempt_cookie=self._cookies.attempt_cookie(attempt.binding, max_age=self._policy.attempt_ttl_seconds),
            attempt=attempt,
        )

    async def complete_login(
        self,
        params: Mapping[str, Any],
        *,
        attempt_binding: str | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LoginCompleted:
        state = str(params.get("state") or "").strip()
        if not state:
            raise LoginAttemptRejected("attempt_missing", "the callback names no login attempt")
        attempt = await self._attempts.take(state)
        if attempt is None:
            raise LoginAttemptRejected("attempt_missing", "the login attempt is unknown, expired, or already used")
        presented = str(attempt_binding or "")
        if not presented or not hmac.compare_digest(presented.encode("utf-8"), attempt.binding.encode("utf-8")):
            raise LoginAttemptRejected("binding_mismatch", "the sign-in was started by another browser")
        try:
            identity = await self._upstream.complete(params, attempt)
        except UpstreamRejected as exc:
            raise LoginRejected(exc.reason, exc.detail) from exc
        now = self._now()
        expires_at = self._policy.expiry_after(now=now, issued_at=now)
        session = await self._backend.login_or_register(
            identity,
            expires_at=expires_at,
            metadata={"upstream": self._upstream.name, **dict(attempt.metadata), **dict(metadata or {})},
        )
        return LoginCompleted(
            session=session,
            identity=identity,
            session_cookie=self._cookies.session_cookie(session.token, max_age=self._policy.max_ttl_seconds),
            clear_attempt_cookie=self._cookies.clear_attempt_cookie(),
            redirect_to=attempt.next_path,
        )

    async def validate_request(self, token: str | None, *, now: int | None = None) -> SessionState | None:
        """The live session for a cookie token, extended when due. ``None``
        for no token, a bad token, or a session that is gone."""
        if not token:
            return None
        moment = int(now if now is not None else self._now())
        state = await self._backend.validate(token, now=moment)
        if state is None:
            return None
        if moment - state.last_seen_at < self._policy.touch_interval_seconds:
            return state
        target = self._policy.expiry_after(now=moment, issued_at=state.issued_at)
        if target <= state.expires_at:
            return state
        return await self._backend.touch(state.session_id, expires_at=target, now=moment)

    async def logout(self, token: str | None, *, post_logout_redirect: str = "") -> LogoutCompleted:
        ended = bool(token) and await self._backend.logout(str(token))
        return LogoutCompleted(
            ended=ended,
            clear_session_cookie=self._cookies.clear_session_cookie(),
            # The upstream's post-logout target is a registered absolute URL the
            # host configures, not a browser-supplied path; it passes as given.
            upstream_logout_url=self._upstream.logout_url(post_logout_redirect=str(post_logout_redirect or "")),
        )


__all__ = [
    "BrowserSessionFlow",
    "LoginAttemptRejected",
    "LoginCompleted",
    "LoginRedirect",
    "LoginRejected",
    "LogoutCompleted",
]

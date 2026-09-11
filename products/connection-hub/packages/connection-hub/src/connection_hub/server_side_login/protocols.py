# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The injection points. A host implements these; the flow never sees the
host's storage, framework, or settings.

Every method that may touch a store is ``async``; a host with a synchronous
store wraps it. Nothing here raises host exceptions through the flow: a
backend answers ``None`` for "no such session", an upstream raises
``UpstreamRejected`` with a reason a person can read.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from connection_hub.server_side_login.model import (
    CookieSpec,
    IssuedSession,
    LoginAttempt,
    SessionState,
    VerifiedIdentity,
)


class UpstreamRejected(Exception):
    """The upstream could not prove the identity; ``reason`` is a short code
    (``state_mismatch``, ``nonce_mismatch``, ``token_invalid``, ...)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@runtime_checkable
class LoginAttemptStore(Protocol):
    """One-time login attempts keyed by ``state``. ``take`` returns the
    attempt at most once: a second callback with the same state finds
    nothing."""

    async def put(self, attempt: LoginAttempt) -> None: ...

    async def take(self, state: str) -> LoginAttempt | None: ...


@runtime_checkable
class SessionBackend(Protocol):
    """Sessions and the platform user record behind them.

    ``login_or_register`` registers the platform user for a verified identity
    when it is new, updates it when it is known, and issues a session whose
    token the browser will carry. ``validate`` answers the live state for a
    token or ``None``. ``touch`` moves a session's expiry (the sliding
    renewal); it returns the new state or ``None`` when the session is gone.
    ``logout`` ends one session; ``True`` when one was ended.
    """

    async def login_or_register(
        self,
        identity: VerifiedIdentity,
        *,
        expires_at: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> IssuedSession: ...

    async def validate(self, token: str, *, now: int) -> SessionState | None: ...

    async def touch(self, session_id: str, *, expires_at: int, now: int) -> SessionState | None: ...

    async def logout(self, token: str) -> bool: ...


@runtime_checkable
class UpstreamIdentity(Protocol):
    """Who proves the identity.

    ``begin`` returns where to send the browser for this attempt, or an empty
    string when the upstream does not redirect (a credential is posted to the
    host instead, as Google Identity Services does). ``complete`` turns the
    callback parameters into a verified identity or raises
    ``UpstreamRejected``.
    """

    @property
    def name(self) -> str: ...

    async def begin(self, attempt: LoginAttempt) -> str: ...

    async def complete(self, params: Mapping[str, Any], attempt: LoginAttempt) -> VerifiedIdentity: ...

    def logout_url(self, *, post_logout_redirect: str = "") -> str: ...


@runtime_checkable
class CookiePolicy(Protocol):
    """Cookie names and attributes, built from deployment configuration."""

    def session_cookie(self, token: str, *, max_age: int) -> CookieSpec: ...

    def clear_session_cookie(self) -> CookieSpec: ...

    def attempt_cookie(self, binding: str, *, max_age: int) -> CookieSpec: ...

    def clear_attempt_cookie(self) -> CookieSpec: ...

    def return_cookie(self, next_path: str, *, max_age: int) -> CookieSpec: ...

    def clear_return_cookie(self) -> CookieSpec: ...


@runtime_checkable
class IdTokenVerifier(Protocol):
    """Verifies an upstream ID token's signature and standard claims and
    returns its claims. Raises ``UpstreamRejected`` otherwise."""

    async def verify(self, id_token: str, *, audience: str, issuer: str = "") -> Mapping[str, Any]: ...


__all__ = [
    "CookiePolicy",
    "IdTokenVerifier",
    "LoginAttemptStore",
    "SessionBackend",
    "UpstreamIdentity",
    "UpstreamRejected",
]

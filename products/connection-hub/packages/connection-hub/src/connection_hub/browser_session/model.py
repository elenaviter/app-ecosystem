# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Data types of the browser session. Plain, frozen, host-neutral."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_IDLE_TTL_SECONDS = 12 * 3600
DEFAULT_MAX_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_ATTEMPT_TTL_SECONDS = 10 * 60
DEFAULT_TOUCH_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class SessionPolicy:
    """How long a session lives.

    ``idle_ttl_seconds``: the session ends this long after its last request.
    ``max_ttl_seconds``: the session ends this long after sign-in, whatever
    the activity. ``touch_interval_seconds``: a request extends the session
    only when the last extension is at least this old, so a busy page does
    not write the store on every call. ``attempt_ttl_seconds``: how long a
    started login may take before its one-time attempt expires.
    """

    idle_ttl_seconds: int = DEFAULT_IDLE_TTL_SECONDS
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS
    touch_interval_seconds: int = DEFAULT_TOUCH_INTERVAL_SECONDS
    attempt_ttl_seconds: int = DEFAULT_ATTEMPT_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.idle_ttl_seconds <= 0 or self.max_ttl_seconds <= 0:
            raise ValueError("session ttls must be positive")
        if self.idle_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("idle ttl cannot exceed the maximum ttl")
        if self.touch_interval_seconds < 0 or self.attempt_ttl_seconds <= 0:
            raise ValueError("touch interval must be non-negative and attempt ttl positive")

    def expiry_after(self, *, now: int, issued_at: int) -> int:
        """The expiry a request at ``now`` earns: the idle limit from now,
        never past the maximum from sign-in."""
        return min(now + self.idle_ttl_seconds, issued_at + self.max_ttl_seconds)


@dataclass(frozen=True)
class LoginAttempt:
    """One started login: what the callback must present and where it goes.

    ``state`` is the one-time key the upstream echoes back. ``binding`` is a
    secret the browser holds in a short-lived cookie, so the callback that
    completes the attempt is the browser that started it. ``code_verifier``
    is the PKCE secret; ``nonce`` binds the ID token to this attempt.
    """

    state: str
    binding: str
    nonce: str
    code_verifier: str
    next_path: str
    created_at: int
    expires_at: int
    upstream: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifiedIdentity:
    """What an upstream proved about the person signing in."""

    provider: str
    subject: str
    email: str = ""
    email_verified: bool = False
    name: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def canonical_subject(self) -> str:
        """``<provider>:<subject>``, the platform-side identity key."""
        return f"{self.provider}:{self.subject}"


@dataclass(frozen=True)
class IssuedSession:
    """A session the backend created for a verified identity."""

    token: str
    session_id: str
    subject: str
    issued_at: int
    expires_at: int
    user: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    """A live session as the backend knows it now."""

    session_id: str
    subject: str
    issued_at: int
    expires_at: int
    last_seen_at: int
    provider: str = ""
    provider_subject: str = ""
    user: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CookieSpec:
    """One Set-Cookie the host writes verbatim. ``max_age`` of ``0`` clears."""

    name: str
    value: str
    max_age: int
    secure: bool = True
    http_only: bool = True
    same_site: str = "lax"
    path: str = "/"
    domain: str = ""

    @property
    def clears(self) -> bool:
        return self.max_age <= 0

# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from connection_hub.server_side_login.cookies import StandardCookiePolicy
from connection_hub.server_side_login.memory import MemoryLoginAttemptStore, MemorySessionBackend
from connection_hub.server_side_login.model import LoginAttempt, VerifiedIdentity
from connection_hub.server_side_login.protocols import UpstreamRejected


class Clock:
    def __init__(self, start: int = 1_800_000_000) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


class FakeUpstream:
    """Redirects to a fake issuer; completes when the callback carries the
    attempt's state and a code, with the nonce echoed back like an ID token."""

    name = "fake"

    def __init__(self, *, subject: str = "sub-1", email: str = "person@example.test", reject: str = "") -> None:
        self.subject = subject
        self.email = email
        self.reject = reject
        self.begun: list[LoginAttempt] = []
        self.completed: list[LoginAttempt] = []

    async def begin(self, attempt: LoginAttempt) -> str:
        self.begun.append(attempt)
        return f"https://idp.test/authorize?state={attempt.state}&nonce={attempt.nonce}"

    async def complete(self, params, attempt: LoginAttempt) -> VerifiedIdentity:
        self.completed.append(attempt)
        if self.reject:
            raise UpstreamRejected(self.reject, "rejected by the test upstream")
        if params.get("state") != attempt.state:
            raise UpstreamRejected("state_mismatch")
        if params.get("nonce") != attempt.nonce:
            raise UpstreamRejected("nonce_mismatch")
        return VerifiedIdentity(provider="fake", subject=self.subject, email=self.email, email_verified=True, name="Person")

    def logout_url(self, *, post_logout_redirect: str = "") -> str:
        return f"https://idp.test/logout?to={post_logout_redirect}" if post_logout_redirect else "https://idp.test/logout"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def backend(clock: Clock) -> MemorySessionBackend:
    return MemorySessionBackend(secret="unit-secret", clock=clock)


@pytest.fixture
def attempts(clock: Clock) -> MemoryLoginAttemptStore:
    return MemoryLoginAttemptStore(clock=clock)


@pytest.fixture
def cookies() -> StandardCookiePolicy:
    return StandardCookiePolicy(session_name="__Secure-LATC")

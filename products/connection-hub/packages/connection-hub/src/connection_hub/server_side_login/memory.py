# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""In-memory stores and backend: the fakes tests run on, and enough for a
single-process host. A platform injects its own (Redis, its user registry).
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Mapping

from connection_hub.server_side_login import kst1
from connection_hub.server_side_login.model import (
    IssuedSession,
    LoginAttempt,
    SessionState,
    VerifiedIdentity,
)


class MemoryLoginAttemptStore:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._attempts: dict[str, LoginAttempt] = {}
        self._clock = clock

    async def put(self, attempt: LoginAttempt) -> None:
        self._attempts[attempt.state] = attempt

    async def take(self, state: str) -> LoginAttempt | None:
        attempt = self._attempts.pop(str(state or ""), None)
        if attempt is None or attempt.expires_at <= int(self._clock()):
            return None
        return attempt

    def __len__(self) -> int:
        return len(self._attempts)


class MemorySessionBackend:
    """Users keyed by canonical subject, sessions keyed by id, tokens as
    ``kst1`` signed with ``secret``. ``users`` and ``sessions`` are readable
    for assertions."""

    def __init__(self, *, secret: str = "test-secret", clock: Callable[[], float] = time.time) -> None:
        self._secret = secret
        self._clock = clock
        self.users: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}

    async def login_or_register(
        self,
        identity: VerifiedIdentity,
        *,
        expires_at: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> IssuedSession:
        now = int(self._clock())
        subject = identity.canonical_subject
        user = self.users.setdefault(subject, {"sub": subject, "created_at": now, "version": 1, "enabled": True})
        user.update({
            "provider": identity.provider,
            "provider_subject": identity.subject,
            "email": identity.email,
            "name": identity.name,
            "last_login_at": now,
        })
        session_id = secrets.token_urlsafe(16)
        claims = {
            "schema": kst1.CLAIMS_SCHEMA,
            "iss": "memory-session",
            "sid": session_id,
            "sub": subject,
            "provider": identity.provider,
            "provider_subject": identity.subject,
            "ver": user["version"],
            "iat": now,
            "exp": expires_at,
        }
        token = kst1.encode(claims, secret=self._secret)
        self.sessions[session_id] = {
            "session_id": session_id,
            "sub": subject,
            "token_hash": kst1.token_hash(token),
            "issued_at": now,
            "expires_at": expires_at,
            "last_seen_at": now,
            "active": True,
            "metadata": dict(metadata or {}),
        }
        return IssuedSession(
            token=token,
            session_id=session_id,
            subject=subject,
            issued_at=now,
            expires_at=expires_at,
            user=dict(user),
        )

    def _live(self, token: str, now: int) -> dict[str, Any] | None:
        try:
            claims = kst1.decode(token, secret=self._secret)
        except kst1.TokenInvalid:
            return None
        record = self.sessions.get(str(claims.get("sid") or ""))
        if record is None or not record.get("active"):
            return None
        if record["token_hash"] != kst1.token_hash(token) or record["expires_at"] <= now:
            return None
        user = self.users.get(record["sub"])
        if user is None or not user.get("enabled", True) or user.get("version") != claims.get("ver"):
            return None
        return record

    def _state(self, record: Mapping[str, Any]) -> SessionState:
        user = self.users.get(record["sub"]) or {}
        return SessionState(
            session_id=record["session_id"],
            subject=record["sub"],
            issued_at=record["issued_at"],
            expires_at=record["expires_at"],
            last_seen_at=record["last_seen_at"],
            provider=str(user.get("provider") or ""),
            provider_subject=str(user.get("provider_subject") or ""),
            user=dict(user),
        )

    async def validate(self, token: str, *, now: int) -> SessionState | None:
        record = self._live(token, now)
        return self._state(record) if record is not None else None

    async def touch(self, session_id: str, *, expires_at: int, now: int) -> SessionState | None:
        record = self.sessions.get(session_id)
        if record is None or not record.get("active") or record["expires_at"] <= now:
            return None
        record["expires_at"] = int(expires_at)
        record["last_seen_at"] = int(now)
        return self._state(record)

    async def logout(self, token: str) -> bool:
        try:
            claims = kst1.decode(token, secret=self._secret)
        except kst1.TokenInvalid:
            return False
        record = self.sessions.get(str(claims.get("sid") or ""))
        if record is None or not record.get("active"):
            return False
        record["active"] = False
        return True

    async def invalidate_user(self, subject: str) -> int:
        """Ends every session of a user and bumps its version, so tokens
        already in browsers fail validation."""
        user = self.users.get(subject)
        if user is None:
            return 0
        user["version"] = int(user.get("version") or 1) + 1
        ended = 0
        for record in self.sessions.values():
            if record["sub"] == subject and record.get("active"):
                record["active"] = False
                ended += 1
        return ended


__all__ = ["MemoryLoginAttemptStore", "MemorySessionBackend"]

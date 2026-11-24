# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: the turn-token key and the conversation registry, as guard readers.

The delegated guard runs in the serving process (KDCube's ingress), whose
readers are composed once at startup; it never builds the Hub service, so
these come with the readers, not from a module global.

- ``TurnTokenSource`` reads the Hub's grantor-authority secret through a host
  reader, derives the purpose-bound turn-token key at once and keeps only that
  key (the secret also backs grantor proofs; it is never held or logged). The
  key is cached for ``ttl_seconds``; a token whose seal fails is retried once
  after re-reading the secret, at most every ``reread_seconds``, so a rotation
  does not refuse turns while the cache is warm.
- ``LivenessCache`` caches only a positive ``turn_alive`` per (owner,
  conversation, turn) for at most ``ttl_seconds``; a refusal is never delayed.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from connection_hub.delegated_credentials.slices.turns import TurnClaims, TurnRefused, turn_tokens_from_secret

SecretReader = Callable[[], Awaitable[str]]


class TurnTokenSource:
    def __init__(self, reader: SecretReader, *, ttl_seconds: float = 300.0, reread_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._reader, self._ttl, self._reread, self._clock = reader, ttl_seconds, reread_seconds, clock
        self._tokens: Any = None
        self._loaded_at = float("-inf")

    async def _load(self) -> Any:
        try:
            secret = await self._reader()
        except Exception:  # noqa: BLE001 - an unreadable secret is "no key": fail closed
            secret = ""
        self._tokens = turn_tokens_from_secret(secret)  # the secret goes out of scope here
        self._loaded_at = self._clock()
        return self._tokens

    async def tokens(self) -> Any:
        if self._tokens is None or self._clock() - self._loaded_at >= self._ttl:
            await self._load()
        return self._tokens

    async def verify(self, token: str, **expected: Any) -> TurnClaims:
        tokens = await self.tokens()
        if tokens is None:
            raise TurnRefused("conversation_slices_unavailable", retryable=True, status=503)
        try:
            return tokens.verify(token, **expected)
        except TurnRefused as exc:
            if exc.reason != "turn_token_invalid" or self._clock() - self._loaded_at < self._reread:
                raise
        # The seal failed on a key older than the reread window: the secret may have rotated.
        tokens = await self._load()
        if tokens is None:
            raise TurnRefused("conversation_slices_unavailable", retryable=True, status=503)
        return tokens.verify(token, **expected)


class LivenessCache:
    def __init__(self, registry: Any, *, ttl_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._registry, self._ttl, self._clock = registry, ttl_seconds, clock
        self._alive: dict[tuple[str, str, str], float] = {}

    async def conversation(self, **kwargs: Any) -> Any:
        return await self._registry.conversation(**kwargs)

    async def turn_alive(self, *, owner_subject: str, conversation_id: str, turn_id: str) -> bool:
        key = (owner_subject, conversation_id, turn_id)
        seen = self._alive.get(key)
        if seen is not None and self._clock() - seen < self._ttl:
            return True
        alive = bool(await self._registry.turn_alive(owner_subject=owner_subject, conversation_id=conversation_id,
                                                     turn_id=turn_id))
        if alive:
            self._alive[key] = self._clock()
            if len(self._alive) > 4096:
                cutoff = self._clock() - self._ttl
                self._alive = {k: at for k, at in self._alive.items() if at >= cutoff}
        else:
            self._alive.pop(key, None)
        return alive


__all__ = ["LivenessCache", "SecretReader", "TurnTokenSource"]

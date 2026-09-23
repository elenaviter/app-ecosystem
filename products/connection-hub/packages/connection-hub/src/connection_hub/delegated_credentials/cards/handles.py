# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Pre-cutover Redis credential handles for a delegated Card.

This representation remains the explicit migration source and compatibility
adapter. PostgreSQL deployments use minimized Card-handle metadata, OAuth
bearer hashes in OAuth authority tables, and host secret custody for a resident
agent bearer; they never write this raw JSON record.
"""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.cache_io import (
    decode_cache_value,
    encode_cache_value,
)
from connection_hub.delegated_credentials.cards.model import (
    CardCredentialHandles,
)
from connection_hub.delegated_credentials.cards.store import (
    validated_access_id,
)


class DelegatedCardHandleStore:
    """TTL-managed Redis source used before authority cutover."""

    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._redis = redis
        self._tenant = str(tenant or "").strip()
        self._project = str(project or "").strip()

    def handle_key(self, access_id: str) -> str:
        return (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            f"card-handles:{validated_access_id(access_id)}"
        )

    async def read(self, access_id: str) -> CardCredentialHandles:
        payload = decode_cache_value(await self._redis.get(self.handle_key(access_id)))
        if payload is None:
            return CardCredentialHandles(access_id=str(access_id))
        return CardCredentialHandles.from_mapping(payload)

    async def write(self, handles: CardCredentialHandles, *, ttl_seconds: int) -> None:
        """Store the handles for the card's remaining lifetime.

        Empty handles are removed rather than stored, so a card that holds no
        reusable secret leaves no key behind.
        """
        key = self.handle_key(handles.access_id)
        if handles.empty or ttl_seconds <= 0:
            await self._redis.delete(key)
            return
        await self._redis.set(
            key, encode_cache_value(handles.to_dict()), ex=max(1, int(ttl_seconds))
        )

    async def remove(self, access_id: str) -> None:
        await self._redis.delete(self.handle_key(access_id))


__all__ = ["DelegatedCardHandleStore"]

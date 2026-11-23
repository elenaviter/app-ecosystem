# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The last refused descriptor-agent synchronization, so the editor can say so.

A refused ``agent_capability.sync`` changes no Card, so nothing a Card read
derives can show it; until now only the log did. Each refusal records its code
and time per owner and agent; the next successful sync clears it. The record
is an explanation, never authority: losing it (a Redis restart) only hides the
banner until the next refusal records it again.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.cache_io import (
    decode_cache_value,
    encode_cache_value,
)

LOGGER = logging.getLogger(__name__)

SYNC_OUTCOME_TTL_SECONDS = 30 * 24 * 3600


def _clean(value: Any) -> str:
    return str(value or "").strip()


class AgentSyncOutcomeStore:
    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._redis = redis
        self._tenant = _clean(tenant)
        self._project = _clean(project)

    def key(self, *, grantor_subject: str, application: str, agent_id: str) -> str:
        owner = hashlib.sha256(_clean(grantor_subject).encode("utf-8")).hexdigest()
        agent = hashlib.sha256(
            f"{_clean(application)}\n{_clean(agent_id)}".encode("utf-8")
        ).hexdigest()
        return (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            f"agent-sync-refusal:{owner}:{agent}"
        )

    async def record(
        self,
        result: Mapping[str, Any],
        *,
        grantor_subject: str,
        application: str,
        agent_id: str,
        descriptor_revision: str = "",
    ) -> None:
        """Record a refusal, or clear the last one after a success."""
        if self._redis is None or not _clean(grantor_subject):
            return
        key = self.key(grantor_subject=grantor_subject, application=application, agent_id=agent_id)
        try:
            if result.get("ok") is True:
                await self._redis.delete(key)
                return
            error = result.get("error")
            code = _clean(error.get("code") if isinstance(error, Mapping) else error)
            await self._redis.set(
                key,
                encode_cache_value({
                    "error": code or "agent_capability_sync_refused",
                    "reason": _clean(result.get("reason"))[:200],
                    "retryable": bool(result.get("retryable")),
                    "at": int(time.time()),
                    "descriptor_revision": _clean(descriptor_revision)[:128],
                }),
                ex=SYNC_OUTCOME_TTL_SECONDS,
            )
        except Exception:
            LOGGER.warning(
                "[agent-capability-sync] refusal record not written application=%s agent=%s",
                application,
                agent_id,
                exc_info=True,
            )

    async def read(
        self,
        *,
        grantor_subject: str,
        application: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        if self._redis is None or not _clean(grantor_subject):
            return None
        try:
            return decode_cache_value(await self._redis.get(
                self.key(grantor_subject=grantor_subject, application=application, agent_id=agent_id)
            ))
        except Exception:
            return None

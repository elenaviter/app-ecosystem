from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from connection_hub.one_time_state import (
    OneTimeStateError,
    RedisRunBoundPointerStore,
    state_digest,
)

DEVICE_PROOF_NONCE_TTL_SECONDS = 60
DEVICE_PROOF_REPLAY_TTL_SECONDS = 120
_PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class DeviceProofStateError(RuntimeError):
    """A nonce or replay reservation could not be trusted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RedisDeviceProofState:
    """One-use proof nonces fenced to one Redis run.

    The shared run-bound pointer primitive invalidates a restored nonce after
    restart or failover. A separate digest-keyed JTI reservation keeps proof
    replay state bounded and contains no credential material.
    """

    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._redis = redis
        scope = hashlib.sha256(
            f"{str(tenant)}\0{str(project)}".encode("utf-8")
        ).hexdigest()
        self._prefix = f"connection-hub:device-proof:{{{scope}}}"
        self._pointers = RedisRunBoundPointerStore(
            redis,
            prefix=f"{self._prefix}:nonce",
        )

    @staticmethod
    def _purpose(value: str) -> str:
        purpose = str(value or "").strip().lower()
        if not _PURPOSE_RE.fullmatch(purpose):
            raise DeviceProofStateError("device_proof_purpose_invalid")
        return purpose

    def _jti_key(self, jti: str) -> str:
        value = str(jti or "").strip()
        if not value or len(value) > 128:
            raise DeviceProofStateError("device_proof_jti_invalid")
        return f"{self._prefix}:jti:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    async def issue_nonce(
        self,
        *,
        purpose: str,
        ttl_seconds: int = DEVICE_PROOF_NONCE_TTL_SECONDS,
    ) -> str:
        normalized = self._purpose(purpose)
        ttl = max(1, min(int(ttl_seconds), 300))
        nonce = secrets.token_urlsafe(32)
        try:
            await self._pointers.put(
                digest=state_digest(nonce),
                record={
                    "schema": "connection_hub.device_proof_nonce.v1",
                    "purpose": normalized,
                },
                ttl_seconds=ttl,
            )
        except OneTimeStateError as exc:
            raise DeviceProofStateError(exc.reason) from exc
        except Exception as exc:
            raise DeviceProofStateError("device_proof_state_unavailable") from exc
        return nonce

    async def claim(
        self,
        *,
        nonce: str,
        jti: str,
        purpose: str,
        replay_ttl_seconds: int = DEVICE_PROOF_REPLAY_TTL_SECONDS,
    ) -> None:
        normalized = self._purpose(purpose)
        value = str(nonce or "").strip()
        if not value or len(value) > 1024:
            raise DeviceProofStateError("device_proof_nonce_invalid")
        try:
            claimed = await self._pointers.claim(digest=state_digest(value))
        except OneTimeStateError as exc:
            raise DeviceProofStateError(exc.reason) from exc
        except Exception as exc:
            raise DeviceProofStateError("device_proof_state_unavailable") from exc
        if claimed is None:
            raise DeviceProofStateError("device_proof_nonce_unknown")
        record = dict(claimed.record)
        if not claimed.belongs_to_current_run:
            raise DeviceProofStateError("device_proof_nonce_generation_changed")
        if (
            record.get("schema") != "connection_hub.device_proof_nonce.v1"
            or str(record.get("purpose") or "") != normalized
        ):
            raise DeviceProofStateError("device_proof_nonce_binding_mismatch")
        ttl = max(1, min(int(replay_ttl_seconds), 300))
        try:
            reserved = await self._redis.set(
                self._jti_key(jti),
                json.dumps({"purpose": normalized}, separators=(",", ":")),
                nx=True,
                ex=ttl,
            )
        except Exception as exc:
            raise DeviceProofStateError("device_proof_state_unavailable") from exc
        if not reserved:
            raise DeviceProofStateError("device_proof_replayed")


__all__ = [
    "DEVICE_PROOF_NONCE_TTL_SECONDS",
    "DEVICE_PROOF_REPLAY_TTL_SECONDS",
    "DeviceProofStateError",
    "RedisDeviceProofState",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral metadata contract for delegated-Card credential handles."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Protocol

from connection_hub.delegated_credentials.cards.store import validated_access_id


HANDLE_STATE_ACTIVE = "active"
HANDLE_STATE_REVOKED = "revoked"
HANDLE_STATE_EXPIRED = "expired"
HANDLE_STATES = (
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_REVOKED,
    HANDLE_STATE_EXPIRED,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CardHandleMetadataConflict(RuntimeError):
    """The caller attempted to mutate a stale metadata revision."""

    def __init__(
        self,
        reason: str,
        *,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.expected_revision = int(expected_revision)
        self.current_revision = int(current_revision)


@dataclass(frozen=True)
class CardHandleMetadata:
    """One Card's minimized credential metadata, never bearer material."""

    access_id: str
    card_revision: int
    expires_at: int
    resident_access_secret_ref: str = ""
    resident_access_sha256: str = ""
    session_id: str = ""
    state: str = HANDLE_STATE_ACTIVE
    revision: int = 0
    created_at: int = 0
    updated_at: int = 0
    retired_at: int = 0

    def validated(self) -> "CardHandleMetadata":
        access_id = validated_access_id(self.access_id)
        card_revision = int(self.card_revision)
        expires_at = int(self.expires_at)
        state = str(self.state or "").strip().lower()
        secret_ref = str(self.resident_access_secret_ref or "").strip()
        fingerprint = str(self.resident_access_sha256 or "").strip().lower()
        session_id = str(self.session_id or "").strip()
        if card_revision < 1:
            raise ValueError("card handle card revision must be positive")
        if expires_at < 1:
            raise ValueError("card handle expiry must be positive")
        if state not in HANDLE_STATES:
            raise ValueError("card handle state is invalid")
        if bool(secret_ref) != bool(fingerprint):
            raise ValueError(
                "resident secret reference and fingerprint must travel together"
            )
        if fingerprint and not _SHA256_PATTERN.fullmatch(fingerprint):
            raise ValueError("resident access fingerprint must be lowercase SHA-256")
        if len(secret_ref) > 512 or any(ord(char) < 32 for char in secret_ref):
            raise ValueError("resident secret reference is invalid")
        if len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
            raise ValueError("session id is invalid")
        return replace(
            self,
            access_id=access_id,
            card_revision=card_revision,
            expires_at=expires_at,
            resident_access_secret_ref=secret_ref,
            resident_access_sha256=fingerprint,
            session_id=session_id,
            state=state,
            revision=max(0, int(self.revision)),
            created_at=max(0, int(self.created_at)),
            updated_at=max(0, int(self.updated_at)),
            retired_at=max(0, int(self.retired_at)),
        )

    @property
    def payload_identity(self) -> tuple[Any, ...]:
        return (
            self.access_id,
            self.card_revision,
            self.expires_at,
            self.resident_access_secret_ref,
            self.resident_access_sha256,
            self.session_id,
            self.state,
        )

    def serves_at(self, moment: int) -> bool:
        return self.state == HANDLE_STATE_ACTIVE and self.expires_at > int(moment)


@dataclass(frozen=True)
class RetiredResidentSecret:
    """One outgoing host-secret reference awaiting confirmed deletion."""

    access_id: str
    secret_ref: str
    resident_access_sha256: str
    retired_at: int

    def validated(self) -> "RetiredResidentSecret":
        access_id = validated_access_id(self.access_id)
        secret_ref = str(self.secret_ref or "").strip()
        fingerprint = str(self.resident_access_sha256 or "").strip().lower()
        if not secret_ref or len(secret_ref) > 512:
            raise ValueError("retired resident secret reference is invalid")
        if any(ord(char) < 32 for char in secret_ref):
            raise ValueError("retired resident secret reference is invalid")
        if not _SHA256_PATTERN.fullmatch(fingerprint):
            raise ValueError("retired resident fingerprint must be lowercase SHA-256")
        return replace(
            self,
            access_id=access_id,
            secret_ref=secret_ref,
            resident_access_sha256=fingerprint,
            retired_at=max(0, int(self.retired_at)),
        )


class CardHandleMetadataStore(Protocol):
    async def read_current(self, access_id: str) -> CardHandleMetadata | None: ...

    async def read_active(
        self, access_id: str, *, now: int | None = None
    ) -> CardHandleMetadata | None: ...

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata: ...

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> CardHandleMetadata | None: ...

    async def expire_due(
        self, *, now: int | None = None, limit: int = 100
    ) -> list[CardHandleMetadata]: ...

    async def clear_retired_resident_secret(
        self,
        access_id: str,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata | None: ...

    async def list_secret_cleanup_candidates(
        self, *, limit: int = 100
    ) -> list[CardHandleMetadata]: ...

    async def list_retired_secret_cleanup_candidates(
        self, *, limit: int = 100
    ) -> list[RetiredResidentSecret]: ...

    async def delete_retired_secret_record(
        self, *, access_id: str, secret_ref: str
    ) -> bool: ...

    async def purge_terminal(
        self, *, retired_before: int, limit: int = 1000
    ) -> int: ...


__all__ = [
    "CardHandleMetadata",
    "CardHandleMetadataConflict",
    "CardHandleMetadataStore",
    "HANDLE_STATE_ACTIVE",
    "HANDLE_STATE_EXPIRED",
    "HANDLE_STATE_REVOKED",
    "HANDLE_STATES",
    "RetiredResidentSecret",
]

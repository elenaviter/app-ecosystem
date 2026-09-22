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

CLEANUP_SOURCE_PREPARED = "prepared"
CLEANUP_SOURCE_RETIRED = "retired"
CLEANUP_SOURCE_TERMINAL = "terminal"
CLEANUP_SOURCES = (
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
)

PREPARED_SECRET_STATE_INSTALLABLE = "installable"
PREPARED_SECRET_STATE_CLEANUP = "cleanup"
PREPARED_SECRET_STATES = (
    PREPARED_SECRET_STATE_INSTALLABLE,
    PREPARED_SECRET_STATE_CLEANUP,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


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

    def validated(self) -> CardHandleMetadata:
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

    def validated(self) -> RetiredResidentSecret:
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


@dataclass(frozen=True)
class PreparedResidentSecret:
    """Durable address and binding for a host-secret write in progress."""

    access_id: str
    secret_ref: str
    resident_access_sha256: str
    card_revision: int
    created_at: int
    expires_at: int
    state: str = PREPARED_SECRET_STATE_INSTALLABLE

    def validated(self) -> PreparedResidentSecret:
        access_id = validated_access_id(self.access_id)
        secret_ref = str(self.secret_ref or "").strip()
        fingerprint = str(self.resident_access_sha256 or "").strip().lower()
        card_revision = int(self.card_revision)
        created_at = int(self.created_at)
        expires_at = int(self.expires_at)
        state = str(self.state or "").strip().lower()
        if not secret_ref or len(secret_ref) > 512:
            raise ValueError("prepared resident secret reference is invalid")
        if any(ord(char) < 32 for char in secret_ref):
            raise ValueError("prepared resident secret reference is invalid")
        if not _SHA256_PATTERN.fullmatch(fingerprint):
            raise ValueError("prepared resident fingerprint must be lowercase SHA-256")
        if card_revision < 1:
            raise ValueError("prepared resident card revision must be positive")
        if created_at < 1 or expires_at <= created_at:
            raise ValueError("prepared resident secret lifetime is invalid")
        if state not in PREPARED_SECRET_STATES:
            raise ValueError("prepared resident secret state is invalid")
        return replace(
            self,
            access_id=access_id,
            secret_ref=secret_ref,
            resident_access_sha256=fingerprint,
            card_revision=card_revision,
            created_at=created_at,
            expires_at=expires_at,
            state=state,
        )


@dataclass(frozen=True)
class ResidentSecretCleanupClaim:
    """One durable, leased right to attempt host-secret deletion."""

    source: str
    access_id: str
    secret_ref: str
    resident_access_sha256: str
    claim_token: str
    attempt: int
    claimed_at: int
    claim_expires_at: int
    card_revision: int = 0
    created_at: int = 0
    expires_at: int = 0
    metadata_revision: int = 0

    def validated(self) -> ResidentSecretCleanupClaim:
        source = str(self.source or "").strip().lower()
        access_id = validated_access_id(self.access_id)
        secret_ref = str(self.secret_ref or "").strip()
        fingerprint = str(self.resident_access_sha256 or "").strip().lower()
        claim_token = str(self.claim_token or "").strip().lower()
        attempt = int(self.attempt)
        claimed_at = int(self.claimed_at)
        claim_expires_at = int(self.claim_expires_at)
        card_revision = int(self.card_revision)
        created_at = int(self.created_at)
        expires_at = int(self.expires_at)
        metadata_revision = int(self.metadata_revision)
        if source not in CLEANUP_SOURCES:
            raise ValueError("resident secret cleanup source is invalid")
        if not secret_ref or len(secret_ref) > 512:
            raise ValueError("resident secret cleanup reference is invalid")
        if any(ord(char) < 32 for char in secret_ref):
            raise ValueError("resident secret cleanup reference is invalid")
        if not _SHA256_PATTERN.fullmatch(fingerprint):
            raise ValueError("resident secret cleanup fingerprint is invalid")
        if not _CLAIM_TOKEN_PATTERN.fullmatch(claim_token):
            raise ValueError("resident secret cleanup claim token is invalid")
        if attempt < 1:
            raise ValueError("resident secret cleanup attempt must be positive")
        if claimed_at < 1 or claim_expires_at <= claimed_at:
            raise ValueError("resident secret cleanup claim lifetime is invalid")
        if (
            source in {CLEANUP_SOURCE_PREPARED, CLEANUP_SOURCE_TERMINAL}
            and (card_revision < 1 or expires_at < 1)
        ):
            raise ValueError("resident secret cleanup binding is incomplete")
        if source == CLEANUP_SOURCE_PREPARED and (
            created_at < 1 or expires_at <= created_at
        ):
            raise ValueError("prepared resident secret cleanup lifetime is invalid")
        if source == CLEANUP_SOURCE_TERMINAL and metadata_revision < 1:
            raise ValueError("terminal resident secret cleanup revision is invalid")
        return replace(
            self,
            source=source,
            access_id=access_id,
            secret_ref=secret_ref,
            resident_access_sha256=fingerprint,
            claim_token=claim_token,
            attempt=attempt,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
            card_revision=max(0, card_revision),
            created_at=max(0, created_at),
            expires_at=max(0, expires_at),
            metadata_revision=max(0, metadata_revision),
        )


@dataclass(frozen=True)
class ResidentSecretCleanupAcknowledgement:
    """The fenced database result after confirmed host deletion."""

    acknowledged: bool
    metadata: CardHandleMetadata | None = None


@dataclass(frozen=True)
class CardHandleMutationResult:
    """The committed metadata row and exact outgoing cleanup address."""

    metadata: CardHandleMetadata
    retired_secret: RetiredResidentSecret | None = None


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

    async def prepare_resident_secret(
        self,
        prepared: PreparedResidentSecret,
    ) -> bool: ...

    async def install_prepared_resident_secret(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMutationResult: ...

    async def claim_prepared_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: PreparedResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]: ...

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

    async def claim_retired_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: RetiredResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]: ...

    async def claim_terminal_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: CardHandleMetadata | None = None,
    ) -> list[ResidentSecretCleanupClaim]: ...

    async def acknowledge_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
    ) -> ResidentSecretCleanupAcknowledgement: ...

    async def defer_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
        *,
        now: int,
        reason: str,
    ) -> bool: ...

    async def purge_terminal(
        self, *, retired_before: int, limit: int = 1000
    ) -> int: ...


__all__ = [
    "CLEANUP_SOURCES",
    "CLEANUP_SOURCE_PREPARED",
    "CLEANUP_SOURCE_RETIRED",
    "CLEANUP_SOURCE_TERMINAL",
    "HANDLE_STATES",
    "HANDLE_STATE_ACTIVE",
    "HANDLE_STATE_EXPIRED",
    "HANDLE_STATE_REVOKED",
    "PREPARED_SECRET_STATES",
    "PREPARED_SECRET_STATE_CLEANUP",
    "PREPARED_SECRET_STATE_INSTALLABLE",
    "CardHandleMetadata",
    "CardHandleMetadataConflict",
    "CardHandleMetadataStore",
    "CardHandleMutationResult",
    "PreparedResidentSecret",
    "ResidentSecretCleanupAcknowledgement",
    "ResidentSecretCleanupClaim",
    "RetiredResidentSecret",
]

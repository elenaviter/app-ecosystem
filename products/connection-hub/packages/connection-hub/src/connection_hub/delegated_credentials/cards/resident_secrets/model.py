# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Resident delegated-Card bearer envelope and host-custody port."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
)
from connection_hub.delegated_credentials.cards.store import (
    CardStorageError,
    validated_access_id,
)

RESIDENT_SECRET_SCHEMA = "connection_hub.card.resident_access.v1"
RESIDENT_SECRET_MAX_BEARER_BYTES = 65_536
RESIDENT_SECRET_MAX_ENVELOPE_BYTES = 131_072
RESIDENT_SECRET_MAX_BATCH = 10_000

SECRET_REF_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "access_id",
        "card_revision",
        "fingerprint",
        "value",
        "created_at",
        "expires_at",
    }
)


class ResidentSecretError(RuntimeError):
    """A resident bearer could not be stored, resolved, or cleaned safely."""

    def __init__(
        self,
        reason: str,
        *,
        access_id: str = "",
        secret_ref: str = "",
        operation_error_type: str = "",
        cleanup_error_type: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.access_id = str(access_id or "")
        self.secret_ref = str(secret_ref or "")
        self.operation_error_type = str(operation_error_type or "")
        self.cleanup_error_type = str(cleanup_error_type or "")


class ResidentSecretStore(Protocol):
    """Host-owned custody for bounded, expiring resident-secret records.

    References are caller-generated 128-bit values. ``create`` is atomic and
    create-only: it returns ``False`` when a record already exists and never
    changes that record, and returns ``True`` only when this call created it.
    An exception leaves the create outcome unknown. ``delete`` is idempotent,
    and ``get`` distinguishes an absent record from provider unavailability by
    returning ``None`` only for absence and raising for an unavailable read.
    """

    async def create(
        self,
        *,
        secret_ref: str,
        value: str,
        expires_at: int,
    ) -> bool: ...

    async def get(self, *, secret_ref: str) -> str | None: ...

    async def delete(self, *, secret_ref: str) -> None: ...

    async def purge_expired(self, *, now: int, limit: int) -> int: ...


def validated_batch_limit(value: int) -> int:
    limit = int(value)
    if limit < 1 or limit > RESIDENT_SECRET_MAX_BATCH:
        raise ValueError("resident-secret batch limit is invalid")
    return limit


def resident_bearer_fingerprint(value: str) -> str:
    bearer = _validated_bearer(value)
    return hashlib.sha256(bearer.encode("ascii")).hexdigest()


def _validated_bearer(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ResidentSecretError("resident_secret_bearer_invalid")
    if not value.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ResidentSecretError("resident_secret_bearer_invalid")
    if len(value.encode("ascii")) > RESIDENT_SECRET_MAX_BEARER_BYTES:
        raise ResidentSecretError("resident_secret_bearer_too_large")
    return value


def _positive_integer(value: Any, *, reason: str) -> int:
    if type(value) is not int or value < 1:
        raise ResidentSecretError(reason)
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResidentSecretError("resident_secret_envelope_duplicate_field")
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> None:
    raise ResidentSecretError("resident_secret_envelope_invalid")


@dataclass(frozen=True)
class ResidentSecretEnvelope:
    """The only recoverable resident bearer representation."""

    access_id: str
    card_revision: int
    fingerprint: str
    value: str = field(repr=False)
    created_at: int = 0
    expires_at: int = 0

    @classmethod
    def create(
        cls,
        *,
        access_id: str,
        card_revision: int,
        value: str,
        created_at: int,
        expires_at: int,
    ) -> ResidentSecretEnvelope:
        bearer = _validated_bearer(value)
        return cls(
            access_id=access_id,
            card_revision=card_revision,
            fingerprint=resident_bearer_fingerprint(bearer),
            value=bearer,
            created_at=created_at,
            expires_at=expires_at,
        ).validated()

    def validated(self) -> ResidentSecretEnvelope:
        if not isinstance(self.access_id, str):
            raise ResidentSecretError("resident_secret_access_id_invalid")
        try:
            access_id = validated_access_id(self.access_id)
        except (TypeError, ValueError, CardStorageError) as exc:
            raise ResidentSecretError("resident_secret_access_id_invalid") from exc
        card_revision = _positive_integer(
            self.card_revision,
            reason="resident_secret_card_revision_invalid",
        )
        created_at = _positive_integer(
            self.created_at,
            reason="resident_secret_created_at_invalid",
        )
        expires_at = _positive_integer(
            self.expires_at,
            reason="resident_secret_expires_at_invalid",
        )
        if expires_at <= created_at:
            raise ResidentSecretError("resident_secret_expiry_invalid")
        bearer = _validated_bearer(self.value)
        if not isinstance(self.fingerprint, str):
            raise ResidentSecretError("resident_secret_fingerprint_invalid")
        fingerprint = self.fingerprint.strip().lower()
        if not _SHA256_PATTERN.fullmatch(fingerprint):
            raise ResidentSecretError("resident_secret_fingerprint_invalid")
        actual_fingerprint = resident_bearer_fingerprint(bearer)
        if not hmac.compare_digest(fingerprint, actual_fingerprint):
            raise ResidentSecretError("resident_secret_fingerprint_mismatch")
        return ResidentSecretEnvelope(
            access_id=access_id,
            card_revision=card_revision,
            fingerprint=fingerprint,
            value=bearer,
            created_at=created_at,
            expires_at=expires_at,
        )

    def to_json(self) -> str:
        envelope = self.validated()
        encoded = json.dumps(
            {
                "schema": RESIDENT_SECRET_SCHEMA,
                "access_id": envelope.access_id,
                "card_revision": envelope.card_revision,
                "fingerprint": envelope.fingerprint,
                "value": envelope.value,
                "created_at": envelope.created_at,
                "expires_at": envelope.expires_at,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > RESIDENT_SECRET_MAX_ENVELOPE_BYTES:
            raise ResidentSecretError("resident_secret_envelope_too_large")
        return encoded

    @classmethod
    def from_json(cls, value: Any) -> ResidentSecretEnvelope:
        if not isinstance(value, str):
            raise ResidentSecretError("resident_secret_envelope_invalid")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ResidentSecretError("resident_secret_envelope_invalid") from exc
        if not encoded or len(encoded) > RESIDENT_SECRET_MAX_ENVELOPE_BYTES:
            raise ResidentSecretError("resident_secret_envelope_too_large")
        try:
            record = json.loads(
                value,
                object_pairs_hook=_json_object,
                parse_constant=_invalid_json_constant,
            )
        except ResidentSecretError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ResidentSecretError("resident_secret_envelope_invalid") from exc
        if not isinstance(record, Mapping) or set(record) != _ENVELOPE_FIELDS:
            raise ResidentSecretError("resident_secret_envelope_invalid")
        if not isinstance(record.get("schema"), str):
            raise ResidentSecretError("resident_secret_envelope_invalid")
        if record["schema"] != RESIDENT_SECRET_SCHEMA:
            raise ResidentSecretError("resident_secret_envelope_schema_mismatch")
        if not isinstance(record.get("access_id"), str):
            raise ResidentSecretError("resident_secret_access_id_invalid")
        if not isinstance(record.get("fingerprint"), str):
            raise ResidentSecretError("resident_secret_fingerprint_invalid")
        return cls(
            access_id=record["access_id"],
            card_revision=record.get("card_revision"),
            fingerprint=record["fingerprint"],
            value=record.get("value"),
            created_at=record.get("created_at"),
            expires_at=record.get("expires_at"),
        ).validated()


@dataclass(frozen=True)
class ResidentSecretCleanupFailure:
    """One visible cleanup obligation that remains durable for retry."""

    access_id: str
    source: str
    phase: str
    reason: str


@dataclass(frozen=True)
class ResidentSecretCleanupResult:
    scanned: int
    completed: int
    failures: tuple[ResidentSecretCleanupFailure, ...] = ()


@dataclass(frozen=True)
class ResidentSecretInstallResult:
    metadata: CardHandleMetadata
    cleanup_failure: ResidentSecretCleanupFailure | None = None


@dataclass(frozen=True)
class ResidentSecretRetirementResult:
    metadata: CardHandleMetadata | None
    cleanup_failure: ResidentSecretCleanupFailure | None = None


__all__ = [
    "RESIDENT_SECRET_MAX_BATCH",
    "RESIDENT_SECRET_MAX_BEARER_BYTES",
    "RESIDENT_SECRET_MAX_ENVELOPE_BYTES",
    "RESIDENT_SECRET_SCHEMA",
    "SECRET_REF_PATTERN",
    "ResidentSecretCleanupFailure",
    "ResidentSecretCleanupResult",
    "ResidentSecretEnvelope",
    "ResidentSecretError",
    "ResidentSecretInstallResult",
    "ResidentSecretRetirementResult",
    "ResidentSecretStore",
    "resident_bearer_fingerprint",
    "validated_batch_limit",
]

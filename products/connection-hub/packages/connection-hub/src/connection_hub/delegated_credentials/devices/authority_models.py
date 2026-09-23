from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


class DeviceAuthorityError(RuntimeError):
    """A profile-device authority transition was refused."""

    def __init__(self, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ProfileDevice:
    device_id: str
    registry_access_id: str
    card_kind: str
    enrollment_family_id: str
    current_family_id: str
    client_id: str
    subject: str
    identity_scope: str
    device_label: str
    public_jwk: Mapping[str, str]
    thumbprint: str
    enrolled_card_revision: int
    revision: int
    state: str
    created_at: datetime | None
    updated_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class DevicePackage:
    package_id: str
    package_kind: str
    attempt_id: str
    device_id: str
    device_revision: int
    family_id: str
    registry_access_id: str
    card_revision: int
    state: str
    ciphertext: str
    fetch_count: int
    revision: int
    created_at: datetime | None
    expires_at: datetime | None
    first_fetched_at: datetime | None
    last_fetched_at: datetime | None
    delivered_at: datetime | None
    terminal_reason: str


@dataclass(frozen=True, slots=True)
class EnrollmentTransition:
    device: ProfileDevice
    package: DevicePackage
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReissueTransition:
    device: ProfileDevice
    attempt: RecoveryAttempt
    package: DevicePackage
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    attempt_id: str
    device_id: str
    device_revision: int
    registry_access_id: str
    card_revision: int
    state: str
    package_id: str
    revision: int
    created_at: datetime | None
    updated_at: datetime | None
    expires_at: datetime | None
    terminal_reason: str


@dataclass(frozen=True, slots=True)
class CardDeviceStatus:
    device: ProfileDevice
    current_family_state: str
    recovery_attempt: RecoveryAttempt | None
    package: DevicePackage | None


__all__ = [
    "CardDeviceStatus",
    "DeviceAuthorityError",
    "DevicePackage",
    "EnrollmentTransition",
    "ProfileDevice",
    "RecoveryAttempt",
    "ReissueTransition",
]

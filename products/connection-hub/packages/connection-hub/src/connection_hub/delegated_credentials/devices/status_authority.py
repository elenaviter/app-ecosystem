from __future__ import annotations

from typing import Any, Mapping

from connection_hub.delegated_credentials.devices._authority_support import (
    _device_package,
    _profile_device,
    _recovery_attempt,
    _required_text,
    _row,
)
from connection_hub.delegated_credentials.devices.authority_models import (
    CardDeviceStatus,
    DevicePackage,
    RecoveryAttempt,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
    TABLE_RECOVERY_ATTEMPTS,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_FAMILIES,
)


def _attempt_from_status(row: Mapping[str, Any]) -> RecoveryAttempt | None:
    if row.get("status_attempt_id") is None:
        return None
    return _recovery_attempt(
        {
            "attempt_id": row.get("status_attempt_id"),
            "device_id": row.get("status_attempt_device_id"),
            "device_revision": row.get("status_attempt_device_revision"),
            "registry_access_id": row.get("status_attempt_access_id"),
            "card_revision": row.get("status_attempt_card_revision"),
            "state": row.get("status_attempt_state"),
            "package_id": row.get("status_attempt_package_id"),
            "revision": row.get("status_attempt_revision"),
            "created_at": row.get("status_attempt_created_at"),
            "updated_at": row.get("status_attempt_updated_at"),
            "expires_at": row.get("status_attempt_expires_at"),
            "terminal_reason": row.get("status_attempt_terminal_reason"),
        }
    )


def _package_from_status(row: Mapping[str, Any]) -> DevicePackage | None:
    if row.get("status_package_id") is None:
        return None
    return _device_package(
        {
            "package_id": row.get("status_package_id"),
            "package_kind": row.get("status_package_kind"),
            "attempt_id": row.get("status_package_attempt_id"),
            "device_id": row.get("status_package_device_id"),
            "device_revision": row.get("status_package_device_revision"),
            "family_id": row.get("status_package_family_id"),
            "registry_access_id": row.get("status_package_access_id"),
            "card_revision": row.get("status_package_card_revision"),
            "state": row.get("status_package_state"),
            "jwe_ciphertext": None,
            "fetch_count": row.get("status_package_fetch_count"),
            "revision": row.get("status_package_revision"),
            "created_at": row.get("status_package_created_at"),
            "expires_at": row.get("status_package_expires_at"),
            "first_fetched_at": row.get("status_package_first_fetched_at"),
            "last_fetched_at": row.get("status_package_last_fetched_at"),
            "delivered_at": row.get("status_package_delivered_at"),
            "terminal_reason": row.get("status_package_terminal_reason"),
        }
    )


class CardStatusAuthorityMixin:
    async def list_card_device_statuses(
        self,
        registry_access_id: str,
    ) -> list[CardDeviceStatus]:
        """Return one canonical Card view without exposing package ciphertext."""

        access_id = _required_text(
            registry_access_id,
            field="registry_access_id",
            maximum=512,
        )
        async with self._pool.acquire() as connection:
            values = await connection.fetch(
                f"""
                SELECT device.*,
                       family.state AS status_family_state,
                       attempt.attempt_id AS status_attempt_id,
                       attempt.device_id AS status_attempt_device_id,
                       attempt.device_revision AS status_attempt_device_revision,
                       attempt.registry_access_id AS status_attempt_access_id,
                       attempt.card_revision AS status_attempt_card_revision,
                       attempt.state AS status_attempt_state,
                       attempt.package_id AS status_attempt_package_id,
                       attempt.revision AS status_attempt_revision,
                       attempt.created_at AS status_attempt_created_at,
                       attempt.updated_at AS status_attempt_updated_at,
                       attempt.expires_at AS status_attempt_expires_at,
                       attempt.terminal_reason AS status_attempt_terminal_reason,
                       package.package_id AS status_package_id,
                       package.package_kind AS status_package_kind,
                       package.attempt_id AS status_package_attempt_id,
                       package.device_id AS status_package_device_id,
                       package.device_revision AS status_package_device_revision,
                       package.family_id AS status_package_family_id,
                       package.registry_access_id AS status_package_access_id,
                       package.card_revision AS status_package_card_revision,
                       package.state AS status_package_state,
                       package.fetch_count AS status_package_fetch_count,
                       package.revision AS status_package_revision,
                       package.created_at AS status_package_created_at,
                       package.expires_at AS status_package_expires_at,
                       package.first_fetched_at AS status_package_first_fetched_at,
                       package.last_fetched_at AS status_package_last_fetched_at,
                       package.delivered_at AS status_package_delivered_at,
                       package.terminal_reason AS status_package_terminal_reason
                FROM {self.schema}.{TABLE_PROFILE_DEVICES} AS device
                JOIN {self.schema}.{TABLE_FAMILIES} AS family
                  ON family.family_id = device.current_family_id
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM {self.schema}.{TABLE_RECOVERY_ATTEMPTS} AS candidate
                    WHERE candidate.device_id = device.device_id
                    ORDER BY candidate.created_at DESC, candidate.attempt_id DESC
                    LIMIT 1
                ) AS attempt ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES} AS candidate
                    WHERE candidate.device_id = device.device_id
                    ORDER BY candidate.created_at DESC, candidate.package_id DESC
                    LIMIT 1
                ) AS package ON TRUE
                WHERE device.registry_access_id = $1
                ORDER BY device.created_at, device.device_id
                """,
                access_id,
            )
        statuses: list[CardDeviceStatus] = []
        for value in values:
            row = _row(value)
            statuses.append(
                CardDeviceStatus(
                    device=_profile_device(row),
                    current_family_state=str(row.get("status_family_state") or ""),
                    recovery_attempt=_attempt_from_status(row),
                    package=_package_from_status(row),
                )
            )
        return statuses


__all__ = ["CardStatusAuthorityMixin"]

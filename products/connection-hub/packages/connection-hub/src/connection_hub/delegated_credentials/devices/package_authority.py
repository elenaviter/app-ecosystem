from __future__ import annotations

import hmac
from datetime import datetime, timezone

from connection_hub.delegated_credentials.devices._authority_support import (
    _CommittedDeviceAuthorityError,
    _authority_transaction,
    _device_package,
    _positive_int,
    _profile_device,
    _required_text,
    _row,
)
from connection_hub.delegated_credentials.devices.authority_models import (
    DeviceAuthorityError,
    DevicePackage,
    ProfileDevice,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_FAMILIES,
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
    TABLE_RECOVERY_ATTEMPTS,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256


class PackageAuthorityMixin:
    async def fetch_package(
        self,
        *,
        package_id: str,
        device_id: str,
        device_thumbprint: str,
        registry_access_id: str,
        card_revision: int,
    ) -> DevicePackage:
        """Return the same encrypted package after revalidating every fence."""

        package_identifier = _required_text(package_id, field="package", maximum=256)
        device_identifier = _required_text(device_id, field="id", maximum=256)
        thumbprint = _required_text(
            device_thumbprint,
            field="thumbprint",
            maximum=128,
        )
        access_id = _required_text(
            registry_access_id,
            field="registry_access_id",
            maximum=512,
        )
        expected_card_revision = _positive_int(
            card_revision,
            field="card_revision",
        )
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                probe = await connection.fetchrow(
                    f"""
                    SELECT device_id, family_id
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE package_id = $1
                    """,
                    package_identifier,
                )
                if probe is None:
                    raise DeviceAuthorityError("device_package_not_found")
                probe_row = _row(probe)
                if str(probe_row.get("device_id") or "") != device_identifier:
                    raise DeviceAuthorityError("device_package_device_mismatch")
                device = await self._lock_device(connection, device_identifier)
                await self._lock_families(
                    connection,
                    [str(probe_row.get("family_id") or "")],
                )
                package_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE package_id = $1
                    FOR UPDATE
                    """,
                    package_identifier,
                )
                package = _row(package_value)
                self._require_device_binding(
                    device,
                    thumbprint=thumbprint,
                    registry_access_id=access_id,
                    expected_revision=int(package.get("device_revision") or 0),
                )
                if str(package.get("state") or "") != "ready":
                    raise DeviceAuthorityError("device_package_terminal")
                if int(package.get("card_revision") or 0) != expected_card_revision:
                    await self._terminalize_package(
                        connection,
                        package,
                        state="superseded",
                        reason="card_revision_changed",
                        invalidate_family=True,
                    )
                    raise _CommittedDeviceAuthorityError(
                        "device_package_card_revision_changed"
                    )
                if package.get("expires_at") <= datetime.now(timezone.utc):
                    await self._terminalize_package(
                        connection,
                        package,
                        state="expired",
                        reason="package_expired",
                        invalidate_family=True,
                    )
                    raise _CommittedDeviceAuthorityError("device_package_expired")
                if not str(package.get("jwe_ciphertext") or ""):
                    raise DeviceAuthorityError("device_package_ciphertext_missing")
                fetched = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_DEVICE_PACKAGES}
                    SET fetch_count = fetch_count + 1,
                        revision = revision + 1,
                        first_fetched_at = COALESCE(first_fetched_at, now()),
                        last_fetched_at = now()
                    WHERE package_id = $1 AND state = 'ready'
                    RETURNING *
                    """,
                    package_identifier,
                )
                return _device_package(_row(fetched))

    async def acknowledge_package(
        self,
        *,
        package_id: str,
        device_id: str,
        device_thumbprint: str,
        registry_access_id: str,
        card_revision: int,
        delivery_nonce: str,
    ) -> DevicePackage:
        """Record delivery only after native custody supplies the sealed nonce."""

        package_identifier = _required_text(package_id, field="package", maximum=256)
        device_identifier = _required_text(device_id, field="id", maximum=256)
        thumbprint = _required_text(
            device_thumbprint,
            field="thumbprint",
            maximum=128,
        )
        access_id = _required_text(
            registry_access_id,
            field="registry_access_id",
            maximum=512,
        )
        expected_card_revision = _positive_int(
            card_revision,
            field="card_revision",
        )
        nonce = _required_text(delivery_nonce, field="delivery_nonce", maximum=1024)
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                probe = await connection.fetchrow(
                    f"""
                    SELECT device_id, family_id
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE package_id = $1
                    """,
                    package_identifier,
                )
                if probe is None:
                    raise DeviceAuthorityError("device_package_not_found")
                probe_row = _row(probe)
                if str(probe_row.get("device_id") or "") != device_identifier:
                    raise DeviceAuthorityError("device_package_device_mismatch")
                device = await self._lock_device(connection, device_identifier)
                await self._lock_families(
                    connection,
                    [str(probe_row.get("family_id") or "")],
                )
                package_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE package_id = $1
                    FOR UPDATE
                    """,
                    package_identifier,
                )
                package = _row(package_value)
                self._require_device_binding(
                    device,
                    thumbprint=thumbprint,
                    registry_access_id=access_id,
                    expected_revision=int(package.get("device_revision") or 0),
                )
                package_state = str(package.get("state") or "")
                if package_state == "delivered":
                    if not hmac.compare_digest(
                        str(package.get("delivery_nonce_sha256") or ""),
                        bearer_sha256(nonce),
                    ):
                        raise DeviceAuthorityError("device_package_nonce_mismatch")
                    return _device_package(package)
                if package_state != "ready":
                    raise DeviceAuthorityError("device_package_terminal")
                if int(package.get("card_revision") or 0) != expected_card_revision:
                    await self._terminalize_package(
                        connection,
                        package,
                        state="superseded",
                        reason="card_revision_changed",
                        invalidate_family=True,
                    )
                    raise _CommittedDeviceAuthorityError(
                        "device_package_card_revision_changed"
                    )
                if package.get("expires_at") <= datetime.now(timezone.utc):
                    await self._terminalize_package(
                        connection,
                        package,
                        state="expired",
                        reason="package_expired",
                        invalidate_family=True,
                    )
                    raise _CommittedDeviceAuthorityError("device_package_expired")
                if not hmac.compare_digest(
                    str(package.get("delivery_nonce_sha256") or ""),
                    bearer_sha256(nonce),
                ):
                    raise DeviceAuthorityError("device_package_nonce_mismatch")
                delivered = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_DEVICE_PACKAGES}
                    SET state = 'delivered',
                        jwe_ciphertext = NULL,
                        revision = revision + 1,
                        delivered_at = now(),
                        terminal_reason = ''
                    WHERE package_id = $1 AND state = 'ready'
                    RETURNING *
                    """,
                    package_identifier,
                )
                attempt_id = str(package.get("attempt_id") or "")
                if attempt_id:
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                        SET state = 'delivered',
                            revision = revision + 1,
                            updated_at = now(),
                            terminal_reason = ''
                        WHERE attempt_id = $1 AND state = 'ready'
                        """,
                        attempt_id,
                    )
                return _device_package(_row(delivered))

    async def revoke_device(self, device_id: str) -> ProfileDevice:
        """Revoke one profile device and only its credential families."""

        identifier = _required_text(device_id, field="id", maximum=256)
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                device = await self._lock_device(connection, identifier)
                if str(device.get("state") or "") == "revoked":
                    return _profile_device(device)
                family_values = await connection.fetch(
                    f"""
                    SELECT family_id
                    FROM {self.schema}.{TABLE_DEVICE_FAMILIES}
                    WHERE device_id = $1
                    ORDER BY family_id
                    """,
                    identifier,
                )
                family_ids = [
                    str(_row(value).get("family_id") or "")
                    for value in family_values
                ]
                await self._lock_families(connection, family_ids)
                package_values = await connection.fetch(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE device_id = $1 AND state = 'ready'
                    ORDER BY package_id
                    FOR UPDATE
                    """,
                    identifier,
                )
                for family_id in family_ids:
                    await self._revoke_family(connection, family_id)
                for value in package_values:
                    await self._terminalize_package(
                        connection,
                        _row(value),
                        state="revoked",
                        reason="device_revoked",
                        invalidate_family=False,
                    )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                    SET state = 'revoked',
                        revision = revision + 1,
                        updated_at = now(),
                        terminal_reason = 'device_revoked'
                    WHERE device_id = $1 AND state IN ('waiting', 'ready')
                    """,
                    identifier,
                )
                value = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_PROFILE_DEVICES}
                    SET state = 'revoked',
                        revision = revision + 1,
                        updated_at = now(),
                        revoked_at = now()
                    WHERE device_id = $1 AND state = 'active'
                    RETURNING *
                    """,
                    identifier,
                )
                return _profile_device(_row(value))

    async def expire_ready_packages(self, *, limit: int = 100) -> int:
        """Lazily sweep bounded ciphertext and invalidate undelivered families."""

        maximum = max(1, min(int(limit), 1000))
        async with self._pool.acquire() as connection:
            candidates = await connection.fetch(
                f"""
                SELECT package_id
                FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                WHERE state = 'ready' AND expires_at <= now()
                ORDER BY expires_at, package_id
                LIMIT $1
                """,
                maximum,
            )
        expired = 0
        for candidate in candidates:
            package_id = str(_row(candidate).get("package_id") or "")
            async with self._pool.acquire() as connection:
                async with _authority_transaction(connection):
                    probe = await connection.fetchrow(
                        f"""
                        SELECT device_id, family_id
                        FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                        WHERE package_id = $1
                        """,
                        package_id,
                    )
                    if probe is None:
                        continue
                    probe_row = _row(probe)
                    await self._lock_device(
                        connection,
                        str(probe_row.get("device_id") or ""),
                    )
                    await self._lock_families(
                        connection,
                        [str(probe_row.get("family_id") or "")],
                    )
                    value = await connection.fetchrow(
                        f"""
                        SELECT *
                        FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                        WHERE package_id = $1
                        FOR UPDATE
                        """,
                        package_id,
                    )
                    package = _row(value)
                    if (
                        str(package.get("state") or "") != "ready"
                        or package.get("expires_at") > datetime.now(timezone.utc)
                    ):
                        continue
                    await self._terminalize_package(
                        connection,
                        package,
                        state="expired",
                        reason="package_expired",
                        invalidate_family=True,
                    )
                    expired += 1
        return expired


__all__ = ["PackageAuthorityMixin"]

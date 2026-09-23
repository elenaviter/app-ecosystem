from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from connection_hub.delegated_credentials.devices._authority_support import (
    _CommittedDeviceAuthorityError,
    _authority_transaction,
    _device_package,
    _expiry,
    _json_object,
    _positive_int,
    _profile_device,
    _recovery_attempt,
    _required_text,
    _row,
)
from connection_hub.delegated_credentials.devices.authority_models import (
    DeviceAuthorityError,
    RecoveryAttempt,
    ReissueTransition,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_FAMILIES,
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
    TABLE_RECOVERY_ATTEMPTS,
)
from connection_hub.delegated_credentials.devices.keys import parse_public_jwk
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256

DEFAULT_RECOVERY_ATTEMPT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_REISSUE_PACKAGE_TTL_SECONDS = 15 * 60


class RecoveryAuthorityMixin:
    async def request_recovery(
        self,
        *,
        device_id: str,
        device_thumbprint: str,
        registry_access_id: str,
        card_revision: int,
        ttl_seconds: int = DEFAULT_RECOVERY_ATTEMPT_TTL_SECONDS,
    ) -> RecoveryAttempt:
        """Create or return the one live host-initiated recovery request."""

        identifier = _required_text(device_id, field="id", maximum=256)
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
        revision = _positive_int(card_revision, field="card_revision")
        expires_at = _expiry(ttl_seconds)
        moment = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                device = await self._lock_device(connection, identifier)
                self._require_device_binding(
                    device,
                    thumbprint=thumbprint,
                    registry_access_id=access_id,
                )
                existing_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                    WHERE device_id = $1 AND state IN ('waiting', 'ready')
                    FOR UPDATE
                    """,
                    identifier,
                )
                if existing_value is not None:
                    existing = _row(existing_value)
                    if (
                        existing.get("expires_at") > moment
                        and int(existing.get("device_revision") or 0)
                        == int(device.get("revision") or 0)
                        and int(existing.get("card_revision") or 0) == revision
                    ):
                        return _recovery_attempt(existing)
                    package_id = str(existing.get("package_id") or "")
                    if package_id:
                        package_probe = await connection.fetchrow(
                            f"""
                            SELECT family_id
                            FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                            WHERE package_id = $1
                            """,
                            package_id,
                        )
                        if package_probe is not None:
                            package_family = str(
                                _row(package_probe).get("family_id") or ""
                            )
                            await self._lock_families(connection, [package_family])
                            package_value = await connection.fetchrow(
                                f"""
                                SELECT *
                                FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                                WHERE package_id = $1
                                FOR UPDATE
                                """,
                                package_id,
                            )
                            if package_value is not None:
                                await self._terminalize_package(
                                    connection,
                                    _row(package_value),
                                    state=(
                                        "expired"
                                        if existing.get("expires_at") <= moment
                                        else "superseded"
                                    ),
                                    reason=(
                                        "recovery_attempt_expired"
                                        if existing.get("expires_at") <= moment
                                        else "recovery_binding_changed"
                                    ),
                                    invalidate_family=True,
                                )
                    else:
                        await connection.execute(
                            f"""
                            UPDATE {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                            SET state = $2,
                                revision = revision + 1,
                                updated_at = now(),
                                terminal_reason = $3
                            WHERE attempt_id = $1 AND state = 'waiting'
                            """,
                            str(existing.get("attempt_id") or ""),
                            (
                                "expired"
                                if existing.get("expires_at") <= moment
                                else "superseded"
                            ),
                            (
                                "recovery_attempt_expired"
                                if existing.get("expires_at") <= moment
                                else "recovery_binding_changed"
                            ),
                        )

                attempt_id = f"orcv_{uuid.uuid4().hex}"
                value = await connection.fetchrow(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_RECOVERY_ATTEMPTS} (
                        attempt_id, device_id, device_revision,
                        registry_access_id, card_revision, state, expires_at
                    ) VALUES ($1, $2, $3, $4, $5, 'waiting', $6)
                    RETURNING *
                    """,
                    attempt_id,
                    identifier,
                    int(device.get("revision") or 0),
                    access_id,
                    revision,
                    expires_at,
                )
                return _recovery_attempt(_row(value))

    async def prepare_reissue(
        self,
        *,
        attempt_id: str,
        registry_access_id: str,
        card_revision: int,
        access_token: str,
        access_record: Mapping[str, Any],
        token_response: Mapping[str, Any],
        replacement_record: Mapping[str, Any] | None = None,
        refresh_ttl_seconds: int,
        access_ttl_seconds: int,
        package_ttl_seconds: int = DEFAULT_REISSUE_PACKAGE_TTL_SECONDS,
    ) -> ReissueTransition:
        """Replace one device family and commit its encrypted package atomically."""

        attempt_identifier = _required_text(
            attempt_id,
            field="recovery_attempt",
            maximum=256,
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
        access_bearer = _required_text(
            access_token,
            field="access_token",
            maximum=65536,
        )
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                probe = await connection.fetchrow(
                    f"""
                    SELECT device_id
                    FROM {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                    WHERE attempt_id = $1
                    """,
                    attempt_identifier,
                )
                if probe is None:
                    raise DeviceAuthorityError("device_recovery_attempt_not_found")
                device = await self._lock_device(
                    connection,
                    str(_row(probe).get("device_id") or ""),
                )
                if str(device.get("state") or "") != "active":
                    raise DeviceAuthorityError("device_revoked")
                if str(device.get("registry_access_id") or "") != access_id:
                    raise DeviceAuthorityError("device_card_mismatch")
                old_family_id = str(device.get("current_family_id") or "")
                families = await self._lock_families(connection, [old_family_id])
                old_family = families.get(old_family_id)
                if old_family is None:
                    raise DeviceAuthorityError("device_current_family_missing")
                generation_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    WHERE generation_id = $1
                    FOR UPDATE
                    """,
                    str(old_family.get("current_generation_id") or ""),
                )
                if generation_value is None:
                    raise DeviceAuthorityError("device_current_generation_missing")
                old_generation = _row(generation_value)
                attempt_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                    WHERE attempt_id = $1
                    FOR UPDATE
                    """,
                    attempt_identifier,
                )
                if attempt_value is None:
                    raise DeviceAuthorityError("device_recovery_attempt_not_found")
                attempt = _row(attempt_value)
                existing_package: dict[str, Any] = {}
                package_identifier = str(attempt.get("package_id") or "")
                if package_identifier:
                    package_value = await connection.fetchrow(
                        f"""
                        SELECT *
                        FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                        WHERE package_id = $1
                        FOR UPDATE
                        """,
                        package_identifier,
                    )
                    existing_package = _row(package_value)
                moment = datetime.now(timezone.utc)
                if existing_package:
                    if (
                        str(existing_package.get("state") or "") == "ready"
                        and existing_package.get("expires_at") > moment
                        and int(existing_package.get("card_revision") or 0)
                        == expected_card_revision
                    ):
                        return ReissueTransition(
                            device=_profile_device(device),
                            attempt=_recovery_attempt(attempt),
                            package=_device_package(existing_package),
                            replayed=True,
                        )
                    if str(existing_package.get("state") or "") == "ready":
                        expired = existing_package.get("expires_at") <= moment
                        await self._terminalize_package(
                            connection,
                            existing_package,
                            state=("expired" if expired else "superseded"),
                            reason=(
                                "reissue_package_expired"
                                if expired
                                else "card_revision_changed"
                            ),
                            invalidate_family=True,
                        )
                        raise _CommittedDeviceAuthorityError(
                            "device_reissue_package_terminal"
                        )
                    raise DeviceAuthorityError("device_reissue_package_terminal")
                if (
                    str(attempt.get("state") or "") != "waiting"
                    or attempt.get("expires_at") <= moment
                ):
                    raise DeviceAuthorityError("device_recovery_attempt_inactive")
                if (
                    int(attempt.get("device_revision") or 0)
                    != int(device.get("revision") or 0)
                    or int(attempt.get("card_revision") or 0)
                    != expected_card_revision
                ):
                    raise DeviceAuthorityError("device_recovery_binding_changed")

                old_record = _json_object(old_generation.get("record"))
                new_family_id = f"ofam_{uuid.uuid4().hex}"
                new_generation_id = f"ogen_{uuid.uuid4().hex}"
                new_refresh_token = secrets.token_urlsafe(40)
                package_id = f"opkg_{uuid.uuid4().hex}"
                delivery_nonce = secrets.token_urlsafe(32)
                next_device_revision = int(device.get("revision") or 0) + 1
                refresh_expires_at = _expiry(refresh_ttl_seconds)
                access_expires_at = _expiry(access_ttl_seconds)
                package_expires_at = _expiry(package_ttl_seconds)
                public_key = parse_public_jwk(device.get("public_jwk"))
                ciphertext = self._credential_package(
                    package_id=package_id,
                    package_kind="reissue",
                    device_id=str(device.get("device_id") or ""),
                    device_revision=next_device_revision,
                    family_id=new_family_id,
                    registry_access_id=access_id,
                    card_revision=expected_card_revision,
                    expires_at=package_expires_at,
                    public_jwk=public_key,
                    token_response=token_response,
                    refresh_token=new_refresh_token,
                    delivery_nonce=delivery_nonce,
                )
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_FAMILIES} (
                        family_id, tenant, project, registry_access_id,
                        card_kind, client_id, subject, identity_scope,
                        current_generation_id, revision, state, expires_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, 1, 'active', $10
                    )
                    """,
                    new_family_id,
                    self.tenant,
                    self.project,
                    access_id,
                    str(device.get("card_kind") or ""),
                    str(device.get("client_id") or ""),
                    str(device.get("subject") or ""),
                    str(device.get("identity_scope") or ""),
                    new_generation_id,
                    refresh_expires_at,
                )
                new_record = {
                    **old_record,
                    **dict(replacement_record or {}),
                    "device_id": str(device.get("device_id") or ""),
                    "device_revision": next_device_revision,
                    "device_thumbprint": str(device.get("thumbprint") or ""),
                    "family_id": new_family_id,
                }
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                        generation_id, family_id, token_sha256, record,
                        revision, state, expires_at
                    ) VALUES ($1, $2, $3, ($4::text)::jsonb, 1, 'active', $5)
                    """,
                    new_generation_id,
                    new_family_id,
                    bearer_sha256(new_refresh_token),
                    json.dumps(new_record, sort_keys=True, separators=(",", ":")),
                    refresh_expires_at,
                )
                await self._revoke_family(
                    connection,
                    old_family_id,
                    binding_state="retired",
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_PROFILE_DEVICES}
                    SET current_family_id = $2,
                        revision = $3,
                        updated_at = now()
                    WHERE device_id = $1 AND state = 'active'
                    """,
                    str(device.get("device_id") or ""),
                    new_family_id,
                    next_device_revision,
                )
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_DEVICE_FAMILIES} (
                        family_id, device_id, device_revision, state
                    ) VALUES ($1, $2, $3, 'current')
                    """,
                    new_family_id,
                    str(device.get("device_id") or ""),
                    next_device_revision,
                )
                await self._insert_access_binding(
                    connection,
                    access_token=access_bearer,
                    access_record=access_record,
                    access_expires_at=access_expires_at,
                    registry_access_id=access_id,
                    family_id=new_family_id,
                    device_id=str(device.get("device_id") or ""),
                    device_revision=next_device_revision,
                )
                package_value = await connection.fetchrow(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_DEVICE_PACKAGES} (
                        package_id, package_kind, attempt_id,
                        device_id, device_revision, family_id,
                        registry_access_id, card_revision, state,
                        jwe_ciphertext, delivery_nonce_sha256, expires_at
                    ) VALUES (
                        $1, 'reissue', $2,
                        $3, $4, $5,
                        $6, $7, 'ready',
                        $8, $9, $10
                    )
                    RETURNING *
                    """,
                    package_id,
                    attempt_identifier,
                    str(device.get("device_id") or ""),
                    next_device_revision,
                    new_family_id,
                    access_id,
                    expected_card_revision,
                    ciphertext,
                    bearer_sha256(delivery_nonce),
                    package_expires_at,
                )
                attempt_value = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                    SET state = 'ready',
                        package_id = $2,
                        device_revision = $3,
                        revision = revision + 1,
                        updated_at = now(),
                        expires_at = $4
                    WHERE attempt_id = $1 AND state = 'waiting'
                    RETURNING *
                    """,
                    attempt_identifier,
                    package_id,
                    next_device_revision,
                    package_expires_at,
                )
                device_value = await self._lock_device(
                    connection,
                    str(device.get("device_id") or ""),
                )
                return ReissueTransition(
                    device=_profile_device(device_value),
                    attempt=_recovery_attempt(_row(attempt_value)),
                    package=_device_package(_row(package_value)),
                    replayed=False,
                )


__all__ = [
    "DEFAULT_RECOVERY_ATTEMPT_TTL_SECONDS",
    "DEFAULT_REISSUE_PACKAGE_TTL_SECONDS",
    "RecoveryAuthorityMixin",
]

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from connection_hub.delegated_credentials.devices.authority_models import (
    DeviceAuthorityError,
    DevicePackage,
    ProfileDevice,
    RecoveryAttempt,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_ACCESS_BINDINGS,
    TABLE_DEVICE_FAMILIES,
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
    TABLE_RECOVERY_ATTEMPTS,
)
from connection_hub.delegated_credentials.devices.keys import parse_public_jwk
from connection_hub.delegated_credentials.devices.packages import (
    encrypt_device_package,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256


def _required_text(value: Any, *, field: str, maximum: int = 4096) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        raise DeviceAuthorityError(f"device_{field}_invalid")
    return candidate


def _positive_int(value: Any, *, field: str) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError) as exc:
        raise DeviceAuthorityError(f"device_{field}_invalid") from exc
    if candidate < 1:
        raise DeviceAuthorityError(f"device_{field}_invalid")
    return candidate


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise DeviceAuthorityError("device_authority_record_invalid")


def _row(value: Any) -> dict[str, Any]:
    return dict(value) if value is not None else {}


def _expiry(ttl_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))


def _profile_device(value: Mapping[str, Any]) -> ProfileDevice:
    return ProfileDevice(
        device_id=str(value.get("device_id") or ""),
        registry_access_id=str(value.get("registry_access_id") or ""),
        card_kind=str(value.get("card_kind") or ""),
        enrollment_family_id=str(value.get("enrollment_family_id") or ""),
        current_family_id=str(value.get("current_family_id") or ""),
        client_id=str(value.get("client_id") or ""),
        subject=str(value.get("subject") or ""),
        identity_scope=str(value.get("identity_scope") or ""),
        device_label=str(value.get("device_label") or ""),
        public_jwk=parse_public_jwk(value.get("public_jwk")),
        thumbprint=str(value.get("thumbprint") or ""),
        enrolled_card_revision=int(value.get("enrolled_card_revision") or 0),
        revision=int(value.get("revision") or 0),
        state=str(value.get("state") or ""),
        created_at=value.get("created_at"),
        updated_at=value.get("updated_at"),
        revoked_at=value.get("revoked_at"),
    )


def _device_package(value: Mapping[str, Any]) -> DevicePackage:
    return DevicePackage(
        package_id=str(value.get("package_id") or ""),
        package_kind=str(value.get("package_kind") or ""),
        attempt_id=str(value.get("attempt_id") or ""),
        device_id=str(value.get("device_id") or ""),
        device_revision=int(value.get("device_revision") or 0),
        family_id=str(value.get("family_id") or ""),
        registry_access_id=str(value.get("registry_access_id") or ""),
        card_revision=int(value.get("card_revision") or 0),
        state=str(value.get("state") or ""),
        ciphertext=str(value.get("jwe_ciphertext") or ""),
        fetch_count=int(value.get("fetch_count") or 0),
        revision=int(value.get("revision") or 0),
        created_at=value.get("created_at"),
        expires_at=value.get("expires_at"),
        first_fetched_at=value.get("first_fetched_at"),
        last_fetched_at=value.get("last_fetched_at"),
        delivered_at=value.get("delivered_at"),
        terminal_reason=str(value.get("terminal_reason") or ""),
    )


def _recovery_attempt(value: Mapping[str, Any]) -> RecoveryAttempt:
    return RecoveryAttempt(
        attempt_id=str(value.get("attempt_id") or ""),
        device_id=str(value.get("device_id") or ""),
        device_revision=int(value.get("device_revision") or 0),
        registry_access_id=str(value.get("registry_access_id") or ""),
        card_revision=int(value.get("card_revision") or 0),
        state=str(value.get("state") or ""),
        package_id=str(value.get("package_id") or ""),
        revision=int(value.get("revision") or 0),
        created_at=value.get("created_at"),
        updated_at=value.get("updated_at"),
        expires_at=value.get("expires_at"),
        terminal_reason=str(value.get("terminal_reason") or ""),
    )


class _CommittedDeviceAuthorityError(DeviceAuthorityError):
    """Refusal raised after a terminal SQL transition must commit."""


@asynccontextmanager
async def _authority_transaction(connection: Any):
    committed_refusal: _CommittedDeviceAuthorityError | None = None
    async with connection.transaction():
        try:
            yield
        except _CommittedDeviceAuthorityError as exc:
            committed_refusal = exc
    if committed_refusal is not None:
        raise DeviceAuthorityError(
            committed_refusal.reason,
            details=committed_refusal.details,
        ) from None


class ProfileDeviceAuthoritySupport:
    def __init__(
        self,
        *,
        pg_pool: Any,
        schema: str,
        tenant: str,
        project: str,
    ) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresProfileDeviceAuthority requires pg_pool")
        self._pool = pg_pool
        self.schema = str(schema)
        self.tenant = str(tenant)
        self.project = str(project)

    async def get_device(self, device_id: str) -> ProfileDevice | None:
        identifier = _required_text(device_id, field="id", maximum=256)
        async with self._pool.acquire() as connection:
            value = await connection.fetchrow(
                f"""
                SELECT *
                FROM {self.schema}.{TABLE_PROFILE_DEVICES}
                WHERE device_id = $1
                """,
                identifier,
            )
        return _profile_device(_row(value)) if value is not None else None

    async def list_card_devices(self, registry_access_id: str) -> list[ProfileDevice]:
        access_id = _required_text(
            registry_access_id,
            field="registry_access_id",
            maximum=512,
        )
        async with self._pool.acquire() as connection:
            values = await connection.fetch(
                f"""
                SELECT *
                FROM {self.schema}.{TABLE_PROFILE_DEVICES}
                WHERE registry_access_id = $1
                ORDER BY created_at, device_id
                """,
                access_id,
            )
        return [_profile_device(_row(value)) for value in values]

    async def _lock_device(self, connection: Any, device_id: str) -> dict[str, Any]:
        value = await connection.fetchrow(
            f"""
            SELECT *
            FROM {self.schema}.{TABLE_PROFILE_DEVICES}
            WHERE device_id = $1
            FOR UPDATE
            """,
            device_id,
        )
        if value is None:
            raise DeviceAuthorityError("device_not_found")
        return _row(value)

    async def _lock_families(
        self,
        connection: Any,
        family_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        identifiers = sorted({str(value) for value in family_ids if str(value)})
        if not identifiers:
            return {}
        values = await connection.fetch(
            f"""
            SELECT *
            FROM {self.schema}.{TABLE_FAMILIES}
            WHERE family_id = ANY($1::text[])
            ORDER BY family_id
            FOR UPDATE
            """,
            identifiers,
        )
        return {
            str(record.get("family_id") or ""): record
            for record in (_row(value) for value in values)
        }

    async def _revoke_family(
        self,
        connection: Any,
        family_id: str,
        *,
        binding_state: str = "revoked",
    ) -> None:
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS} AS access
            SET state = 'revoked',
                revision = revision + 1,
                revoked_at = now(),
                updated_at = now()
            WHERE access.token_sha256 IN (
                SELECT token_sha256
                FROM {self.schema}.{TABLE_DEVICE_ACCESS_BINDINGS}
                WHERE family_id = $1 AND state = 'active'
            ) AND access.state = 'active'
            """,
            family_id,
        )
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_DEVICE_ACCESS_BINDINGS}
            SET state = 'revoked', revoked_at = now()
            WHERE family_id = $1 AND state = 'active'
            """,
            family_id,
        )
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
            SET state = 'revoked', revision = revision + 1, revoked_at = now()
            WHERE family_id = $1 AND state = 'active'
            """,
            family_id,
        )
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_FAMILIES}
            SET state = 'revoked', revision = revision + 1, updated_at = now()
            WHERE family_id = $1 AND state = 'active'
            """,
            family_id,
        )
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_DEVICE_FAMILIES}
            SET state = $2, retired_at = now()
            WHERE family_id = $1 AND state = 'current'
            """,
            family_id,
            binding_state,
        )

    async def _insert_access_binding(
        self,
        connection: Any,
        *,
        access_token: str,
        access_record: Mapping[str, Any],
        access_expires_at: datetime,
        registry_access_id: str,
        family_id: str,
        device_id: str,
        device_revision: int,
    ) -> None:
        digest = bearer_sha256(access_token)
        payload = {
            **dict(access_record),
            "device_id": device_id,
            "device_revision": device_revision,
            "family_id": family_id,
        }
        await connection.execute(
            f"""
            INSERT INTO {self.schema}.{TABLE_ACCESS_BINDINGS} (
                token_sha256, tenant, project, registry_access_id,
                record, state, expires_at
            ) VALUES (
                $1, $2, $3, $4, ($5::text)::jsonb, 'active', $6
            )
            """,
            digest,
            self.tenant,
            self.project,
            registry_access_id,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            access_expires_at,
        )
        await connection.execute(
            f"""
            INSERT INTO {self.schema}.{TABLE_DEVICE_ACCESS_BINDINGS} (
                token_sha256, family_id, device_id, device_revision, state
            ) VALUES ($1, $2, $3, $4, 'active')
            """,
            digest,
            family_id,
            device_id,
            device_revision,
        )

    @staticmethod
    def _credential_package(
        *,
        package_id: str,
        package_kind: str,
        device_id: str,
        device_revision: int,
        family_id: str,
        registry_access_id: str,
        card_revision: int,
        expires_at: datetime,
        public_jwk: Mapping[str, str],
        token_response: Mapping[str, Any],
        refresh_token: str,
        delivery_nonce: str,
    ) -> str:
        response = dict(token_response)
        response["refresh_token"] = refresh_token
        bindings = {
            "schema": "connection_hub.oauth_device_package.binding.v1",
            "package_id": package_id,
            "package_kind": package_kind,
            "device_id": device_id,
            "device_revision": device_revision,
            "family_id": family_id,
            "registry_access_id": registry_access_id,
            "card_revision": card_revision,
            "expires_at": int(expires_at.timestamp()),
        }
        return encrypt_device_package(
            {
                "schema": "connection_hub.oauth_device_credentials.v1",
                "bindings": bindings,
                "token_response": response,
                "delivery_nonce": delivery_nonce,
            },
            public_jwk=public_jwk,
            bindings=bindings,
        )

    @staticmethod
    def _require_device_binding(
        device: Mapping[str, Any],
        *,
        thumbprint: str,
        registry_access_id: str,
        expected_revision: int | None = None,
    ) -> None:
        if str(device.get("state") or "") != "active":
            raise DeviceAuthorityError("device_revoked")
        if str(device.get("thumbprint") or "") != thumbprint:
            raise DeviceAuthorityError("device_key_mismatch")
        if str(device.get("registry_access_id") or "") != registry_access_id:
            raise DeviceAuthorityError("device_card_mismatch")
        if expected_revision is not None and int(device.get("revision") or 0) != int(
            expected_revision
        ):
            raise DeviceAuthorityError("device_revision_changed")

    async def _terminalize_package(
        self,
        connection: Any,
        package: Mapping[str, Any],
        *,
        state: str,
        reason: str,
        invalidate_family: bool,
    ) -> None:
        if state not in {"expired", "revoked", "superseded"}:
            raise ValueError("package terminal state is invalid")
        family_id = str(package.get("family_id") or "")
        transition = await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_DEVICE_PACKAGES}
            SET state = $2,
                jwe_ciphertext = NULL,
                revision = revision + 1,
                terminal_reason = $3
            WHERE package_id = $1 AND state = 'ready'
            """,
            str(package.get("package_id") or ""),
            state,
            reason,
        )
        if str(transition).strip() != "UPDATE 1":
            return
        if invalidate_family:
            await self._revoke_family(connection, family_id)
        attempt_id = str(package.get("attempt_id") or "")
        if attempt_id:
            await connection.execute(
                f"""
                UPDATE {self.schema}.{TABLE_RECOVERY_ATTEMPTS}
                SET state = $2,
                    revision = revision + 1,
                    updated_at = now(),
                    terminal_reason = $3
                WHERE attempt_id = $1 AND state IN ('waiting', 'ready')
                """,
                attempt_id,
                state,
                reason,
            )


__all__ = [
    "ProfileDeviceAuthoritySupport",
    "_CommittedDeviceAuthorityError",
    "_authority_transaction",
    "_device_package",
    "_expiry",
    "_json_object",
    "_positive_int",
    "_profile_device",
    "_recovery_attempt",
    "_required_text",
    "_row",
]

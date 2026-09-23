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
    _required_text,
    _row,
)
from connection_hub.delegated_credentials.devices.authority_models import (
    DeviceAuthorityError,
    EnrollmentTransition,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_FAMILIES,
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
)
from connection_hub.delegated_credentials.devices.keys import (
    jwk_thumbprint,
    parse_public_jwk,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256

DEFAULT_ENROLLMENT_RECEIPT_TTL_SECONDS = 10 * 60


class EnrollmentAuthorityMixin:
    async def enroll_refresh_family(
        self,
        *,
        refresh_token: str,
        expected_generation: str,
        public_jwk: Mapping[str, Any],
        device_label: str,
        registry_access_id: str,
        card_kind: str,
        card_revision: int,
        client_id: str,
        subject: str,
        identity_scope: str,
        replacement_record: Mapping[str, Any],
        access_token: str,
        access_record: Mapping[str, Any],
        token_response: Mapping[str, Any],
        refresh_ttl_seconds: int,
        access_ttl_seconds: int,
        receipt_ttl_seconds: int = DEFAULT_ENROLLMENT_RECEIPT_TTL_SECONDS,
        requested_device_id: str = "",
    ) -> EnrollmentTransition:
        """Bind one family to a key and rotate it in the same SQL transaction."""

        old_token = _required_text(refresh_token, field="refresh_token", maximum=65536)
        generation_expected = _required_text(
            expected_generation,
            field="generation",
            maximum=256,
        )
        access_bearer = _required_text(access_token, field="access_token", maximum=65536)
        access_id = _required_text(
            registry_access_id,
            field="registry_access_id",
            maximum=512,
        )
        kind = _required_text(card_kind, field="card_kind", maximum=64)
        revision = _positive_int(card_revision, field="card_revision")
        expected_client = _required_text(client_id, field="client_id", maximum=4096)
        expected_subject = _required_text(subject, field="subject", maximum=4096)
        scope = str(identity_scope or "").strip()
        label = _required_text(device_label, field="label", maximum=160)
        key = parse_public_jwk(public_jwk)
        thumbprint = jwk_thumbprint(key)
        requested_id = str(requested_device_id or "").strip()
        if requested_id:
            requested_id = _required_text(requested_id, field="id", maximum=256)

        preliminary_family_id = ""
        existing_device_id = ""
        async with self._pool.acquire() as connection:
            async with _authority_transaction(connection):
                preliminary = await connection.fetchrow(
                    f"""
                    SELECT generation.family_id
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                    WHERE generation.token_sha256 = $1
                    """,
                    bearer_sha256(old_token),
                )
                if preliminary is None:
                    raise DeviceAuthorityError("device_refresh_token_unknown")
                preliminary_family_id = str(
                    _row(preliminary).get("family_id") or ""
                )
                binding = await connection.fetchrow(
                    f"""
                    SELECT device_id
                    FROM {self.schema}.{TABLE_DEVICE_FAMILIES}
                    WHERE family_id = $1
                    """,
                    preliminary_family_id,
                )
                if binding is not None:
                    existing_device_id = str(_row(binding).get("device_id") or "")
                if requested_id and existing_device_id and requested_id != existing_device_id:
                    raise DeviceAuthorityError("device_family_already_bound")
                device_id = requested_id or existing_device_id
                device_row: dict[str, Any] = {}
                if device_id:
                    device_row = await self._lock_device(connection, device_id)

                current_family_id = str(device_row.get("current_family_id") or "")
                family_ids = [preliminary_family_id]
                if current_family_id and current_family_id != preliminary_family_id:
                    family_ids.append(current_family_id)
                families = await self._lock_families(connection, family_ids)
                family = families.get(preliminary_family_id)
                if family is None:
                    raise DeviceAuthorityError("device_refresh_family_missing")

                generation_value = await connection.fetchrow(
                    f"""
                    SELECT *
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    WHERE token_sha256 = $1
                    FOR UPDATE
                    """,
                    bearer_sha256(old_token),
                )
                if generation_value is None:
                    raise DeviceAuthorityError("device_refresh_token_unknown")
                generation = _row(generation_value)
                generation_id = str(generation.get("generation_id") or "")
                if generation_id != generation_expected:
                    raise DeviceAuthorityError("device_refresh_generation_changed")

                if str(generation.get("state") or "") == "consumed":
                    receipt_value = await connection.fetchrow(
                        f"""
                        SELECT package.*
                        FROM {self.schema}.{TABLE_DEVICE_PACKAGES} AS package
                        WHERE package.source_generation_id = $1
                          AND package.package_kind = 'enrollment'
                        FOR UPDATE OF package
                        """,
                        generation_id,
                    )
                    if receipt_value is not None:
                        receipt = _row(receipt_value)
                        receipt_device = _row(
                            await connection.fetchrow(
                                f"""
                                SELECT *
                                FROM {self.schema}.{TABLE_PROFILE_DEVICES}
                                WHERE device_id = $1
                                """,
                                str(receipt.get("device_id") or ""),
                            )
                        )
                        if (
                            str(receipt_device.get("state") or "") == "active"
                            and str(receipt_device.get("thumbprint") or "")
                            == thumbprint
                            and (
                                not requested_id
                                or requested_id
                                == str(receipt_device.get("device_id") or "")
                            )
                            and int(receipt_device.get("revision") or 0)
                            == int(receipt.get("device_revision") or 0)
                            and str(receipt_device.get("current_family_id") or "")
                            == str(receipt.get("family_id") or "")
                            and str(receipt.get("state") or "") == "ready"
                            and receipt.get("expires_at") > datetime.now(timezone.utc)
                        ):
                            return EnrollmentTransition(
                                device=_profile_device(receipt_device),
                                package=_device_package(receipt),
                                replayed=True,
                            )
                        if str(receipt.get("state") or "") == "ready":
                            receipt_expired = (
                                receipt.get("expires_at")
                                <= datetime.now(timezone.utc)
                            )
                            await self._terminalize_package(
                                connection,
                                receipt,
                                state=("expired" if receipt_expired else "revoked"),
                                reason=(
                                    "enrollment_receipt_expired"
                                    if receipt_expired
                                    else "enrollment_receipt_binding_changed"
                                ),
                                invalidate_family=True,
                            )
                        else:
                            await self._revoke_family(
                                connection,
                                preliminary_family_id,
                            )
                    else:
                        await self._revoke_family(connection, preliminary_family_id)
                    raise _CommittedDeviceAuthorityError(
                        "device_refresh_token_reused"
                    )

                if (
                    str(generation.get("state") or "") != "active"
                    or generation.get("expires_at") <= datetime.now(timezone.utc)
                    or str(family.get("state") or "") != "active"
                    or family.get("expires_at") <= datetime.now(timezone.utc)
                ):
                    raise DeviceAuthorityError("device_refresh_token_inactive")

                family_record = _json_object(generation.get("record"))
                expected_binding = (
                    access_id,
                    kind,
                    expected_client,
                    expected_subject,
                    scope,
                )
                actual_binding = (
                    str(family.get("registry_access_id") or ""),
                    str(family.get("card_kind") or ""),
                    str(family.get("client_id") or ""),
                    str(family.get("subject") or ""),
                    str(family.get("identity_scope") or ""),
                )
                if actual_binding != expected_binding:
                    raise DeviceAuthorityError("device_profile_binding_mismatch")

                if device_row:
                    if (
                        str(device_row.get("state") or "") != "active"
                        or str(device_row.get("registry_access_id") or "") != access_id
                        or str(device_row.get("client_id") or "") != expected_client
                        or str(device_row.get("subject") or "") != expected_subject
                        or str(device_row.get("identity_scope") or "") != scope
                    ):
                        raise DeviceAuthorityError("device_profile_binding_mismatch")
                    if str(device_row.get("thumbprint") or "") != thumbprint:
                        raise DeviceAuthorityError("device_key_already_enrolled")
                    if existing_device_id:
                        raise DeviceAuthorityError("device_family_already_enrolled")
                    device_revision = int(device_row.get("revision") or 0) + 1
                else:
                    device_id = f"odev_{uuid.uuid4().hex}"
                    device_revision = 1

                if current_family_id and current_family_id != preliminary_family_id:
                    await self._revoke_family(
                        connection,
                        current_family_id,
                        binding_state="retired",
                    )

                new_refresh_token = secrets.token_urlsafe(40)
                new_generation_id = f"ogen_{uuid.uuid4().hex}"
                package_id = f"opkg_{uuid.uuid4().hex}"
                delivery_nonce = secrets.token_urlsafe(32)
                refresh_expires_at = _expiry(refresh_ttl_seconds)
                access_expires_at = _expiry(access_ttl_seconds)
                receipt_expires_at = _expiry(receipt_ttl_seconds)
                ciphertext = self._credential_package(
                    package_id=package_id,
                    package_kind="enrollment",
                    device_id=device_id,
                    device_revision=device_revision,
                    family_id=preliminary_family_id,
                    registry_access_id=access_id,
                    card_revision=revision,
                    expires_at=receipt_expires_at,
                    public_jwk=key,
                    token_response=token_response,
                    refresh_token=new_refresh_token,
                    delivery_nonce=delivery_nonce,
                )

                if device_row:
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_PROFILE_DEVICES}
                        SET current_family_id = $2,
                            revision = $3,
                            updated_at = now()
                        WHERE device_id = $1 AND state = 'active'
                        """,
                        device_id,
                        preliminary_family_id,
                        device_revision,
                    )
                else:
                    await connection.execute(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_PROFILE_DEVICES} (
                            device_id, tenant, project, registry_access_id,
                            card_kind, enrollment_family_id, current_family_id,
                            client_id, subject, identity_scope, device_label,
                            public_jwk, thumbprint, enrolled_card_revision,
                            revision, state
                        ) VALUES (
                            $1, $2, $3, $4,
                            $5, $6, $6,
                            $7, $8, $9, $10,
                            ($11::text)::jsonb, $12, $13,
                            1, 'active'
                        )
                        """,
                        device_id,
                        self.tenant,
                        self.project,
                        access_id,
                        kind,
                        preliminary_family_id,
                        expected_client,
                        expected_subject,
                        scope,
                        label,
                        json.dumps(key, sort_keys=True, separators=(",", ":")),
                        thumbprint,
                        revision,
                    )

                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_DEVICE_FAMILIES} (
                        family_id, device_id, device_revision, state
                    ) VALUES ($1, $2, $3, 'current')
                    """,
                    preliminary_family_id,
                    device_id,
                    device_revision,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET state = 'consumed', revision = revision + 1, consumed_at = now()
                    WHERE generation_id = $1 AND state = 'active'
                    """,
                    generation_id,
                )
                replacement = {
                    **family_record,
                    **dict(replacement_record),
                    "device_id": device_id,
                    "device_revision": device_revision,
                    "family_id": preliminary_family_id,
                    "device_thumbprint": thumbprint,
                }
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                        generation_id, family_id, token_sha256, record,
                        state, expires_at
                    ) VALUES ($1, $2, $3, ($4::text)::jsonb, 'active', $5)
                    """,
                    new_generation_id,
                    preliminary_family_id,
                    bearer_sha256(new_refresh_token),
                    json.dumps(replacement, sort_keys=True, separators=(",", ":")),
                    refresh_expires_at,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET current_generation_id = $2,
                        revision = revision + 1,
                        updated_at = now(),
                        expires_at = $3
                    WHERE family_id = $1 AND state = 'active'
                    """,
                    preliminary_family_id,
                    new_generation_id,
                    refresh_expires_at,
                )
                await self._insert_access_binding(
                    connection,
                    access_token=access_bearer,
                    access_record=access_record,
                    access_expires_at=access_expires_at,
                    registry_access_id=access_id,
                    family_id=preliminary_family_id,
                    device_id=device_id,
                    device_revision=device_revision,
                )
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_DEVICE_PACKAGES} (
                        package_id, package_kind, device_id, device_revision,
                        family_id, source_generation_id, registry_access_id,
                        card_revision, state, jwe_ciphertext,
                        delivery_nonce_sha256, expires_at
                    ) VALUES (
                        $1, 'enrollment', $2, $3,
                        $4, $5, $6,
                        $7, 'ready', $8, $9, $10
                    )
                    """,
                    package_id,
                    device_id,
                    device_revision,
                    preliminary_family_id,
                    generation_id,
                    access_id,
                    revision,
                    ciphertext,
                    bearer_sha256(delivery_nonce),
                    receipt_expires_at,
                )
                final_device = await self._lock_device(connection, device_id)
                package_value = await connection.fetchrow(
                    f"""
                    SELECT * FROM {self.schema}.{TABLE_DEVICE_PACKAGES}
                    WHERE package_id = $1
                    """,
                    package_id,
                )
                return EnrollmentTransition(
                    device=_profile_device(final_device),
                    package=_device_package(_row(package_value)),
                    replayed=False,
                )


__all__ = [
    "DEFAULT_ENROLLMENT_RECEIPT_TTL_SECONDS",
    "EnrollmentAuthorityMixin",
]

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

from connection_hub.delegated_credentials.devices import (
    DeviceAuthorityError,
    DevicePackage,
    decrypt_device_package,
    generate_device_key,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_FAMILIES,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256


def _package_bindings(package: DevicePackage) -> dict[str, Any]:
    return {
        "schema": "connection_hub.oauth_device_package.binding.v1",
        "package_id": package.package_id,
        "package_kind": package.package_kind,
        "device_id": package.device_id,
        "device_revision": package.device_revision,
        "family_id": package.family_id,
        "registry_access_id": package.registry_access_id,
        "card_revision": package.card_revision,
        "expires_at": int(package.expires_at.timestamp()),
    }


@pytest.mark.asyncio
async def test_competing_first_enrollment_with_another_key_fails_closed() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"w247-test-{uuid.uuid4().hex}",
        project="competing-device-enrollment",
    )
    try:
        await store.ensure_schema()
        refresh_token = await store.create_refresh_token(
            {
                "registry_access_id": "aut_card",
                "card_kind": "automation",
                "client_id": "client-1",
                "sub": "user-1",
                "identity_scope": "grantor",
            },
            ttl_seconds=3600,
        )
        state = await store.get_refresh_token_state(refresh_token)
        assert state is not None
        keys = [generate_device_key(), generate_device_key()]

        async def enroll(index: int):
            access_token = f"access-{index}-{uuid.uuid4().hex}"
            return await store.profile_devices.enroll_refresh_family(
                refresh_token=refresh_token,
                expected_generation=state.raw,
                public_jwk=keys[index].public_jwk,
                device_label=f"worker-{index}",
                registry_access_id="aut_card",
                card_kind="automation",
                card_revision=7,
                client_id="client-1",
                subject="user-1",
                identity_scope="grantor",
                replacement_record={"scopes": ["tools:call"]},
                access_token=access_token,
                access_record={"operations": ["tools.call"]},
                token_response={
                    "access_token": access_token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
                refresh_ttl_seconds=3600,
                access_ttl_seconds=3600,
            )

        results = await asyncio.gather(
            enroll(0),
            enroll(1),
            return_exceptions=True,
        )
        transitions = [
            result for result in results if not isinstance(result, Exception)
        ]
        refusals = [result for result in results if isinstance(result, Exception)]
        assert len(transitions) == 1
        assert len(refusals) == 1
        assert isinstance(refusals[0], DeviceAuthorityError)
        assert refusals[0].reason == "device_refresh_token_reused"

        async with pool.acquire() as connection:
            devices = await connection.fetch(
                f"SELECT thumbprint FROM {store.schema}.{TABLE_PROFILE_DEVICES}"
            )
            package = await connection.fetchrow(
                f"""
                SELECT state, jwe_ciphertext, terminal_reason
                FROM {store.schema}.{TABLE_DEVICE_PACKAGES}
                """
            )
            family = await connection.fetchrow(
                f"SELECT state FROM {store.schema}.{TABLE_FAMILIES}"
            )
        assert len(devices) == 1
        assert str(devices[0]["thumbprint"]) in {
            keys[0].thumbprint,
            keys[1].thumbprint,
        }
        assert package["state"] == "revoked"
        assert package["jwe_ciphertext"] is None
        assert package["terminal_reason"] == "enrollment_receipt_binding_changed"
        assert family["state"] == "revoked"
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {store.schema} CASCADE"
            )
        await pool.close()


@pytest.mark.asyncio
async def test_profile_device_lifecycle_executes_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"w247-test-{uuid.uuid4().hex}",
        project="profile-device-authority",
    )
    raw_secrets: set[str] = set()

    async def create_family(*, profile: str, key):
        refresh_token = await store.create_refresh_token(
            {
                "registry_access_id": "aut_card",
                "card_kind": "automation",
                "client_id": "client-1",
                "sub": "user-1",
                "identity_scope": "grantor",
                "scopes": ["tools:call"],
            },
            ttl_seconds=3600,
        )
        raw_secrets.add(refresh_token)
        state = await store.get_refresh_token_state(refresh_token)
        assert state is not None
        access_token = f"access-{profile}-{uuid.uuid4().hex}"
        raw_secrets.add(access_token)

        async def enroll():
            return await store.profile_devices.enroll_refresh_family(
                refresh_token=refresh_token,
                expected_generation=state.raw,
                public_jwk=key.public_jwk,
                device_label=profile,
                registry_access_id="aut_card",
                card_kind="automation",
                card_revision=7,
                client_id="client-1",
                subject="user-1",
                identity_scope="grantor",
                replacement_record={"scopes": ["tools:call"]},
                access_token=access_token,
                access_record={"operations": ["tools.call"]},
                token_response={
                    "access_token": access_token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
                refresh_ttl_seconds=3600,
                access_ttl_seconds=3600,
            )

        first, second = await asyncio.gather(enroll(), enroll())
        assert sorted([first.replayed, second.replayed]) == [False, True]
        assert first.device == second.device
        assert first.package == second.package
        transition = first if not first.replayed else second
        payload = decrypt_device_package(
            transition.package.ciphertext,
            private_jwk=key.private_jwk,
            expected_bindings=_package_bindings(transition.package),
        )
        raw_secrets.add(str(payload["token_response"]["refresh_token"]))
        delivery_nonce = str(payload["delivery_nonce"])
        raw_secrets.add(delivery_nonce)
        delivered = await store.profile_devices.acknowledge_package(
            package_id=transition.package.package_id,
            device_id=transition.device.device_id,
            device_thumbprint=key.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
            delivery_nonce=delivery_nonce,
        )
        assert delivered.state == "delivered"
        assert delivered.ciphertext == ""
        return transition

    try:
        await store.ensure_schema()
        key_a = generate_device_key()
        key_b = generate_device_key()
        enrolled_a = await create_family(profile="worker-a", key=key_a)
        enrolled_b = await create_family(profile="worker-b", key=key_b)

        async with pool.acquire() as connection:
            revision_before_reads = await connection.fetchval(
                f"""
                SELECT revision
                FROM {store.schema}.{TABLE_PROFILE_DEVICES}
                WHERE device_id = $1
                """,
                enrolled_a.device.device_id,
            )
        assert await store.profile_devices.get_device(
            enrolled_a.device.device_id
        ) == enrolled_a.device
        listed = await store.profile_devices.list_card_devices("aut_card")
        assert {device.device_id for device in listed} == {
            enrolled_a.device.device_id,
            enrolled_b.device.device_id,
        }
        async with pool.acquire() as connection:
            revision_after_reads = await connection.fetchval(
                f"""
                SELECT revision
                FROM {store.schema}.{TABLE_PROFILE_DEVICES}
                WHERE device_id = $1
                """,
                enrolled_a.device.device_id,
            )
        assert revision_after_reads == revision_before_reads == 1

        attempt = await store.profile_devices.request_recovery(
            device_id=enrolled_a.device.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
        )
        repeated_attempt = await store.profile_devices.request_recovery(
            device_id=enrolled_a.device.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
        )
        assert repeated_attempt == attempt
        waiting_statuses = await store.profile_devices.list_card_device_statuses(
            "aut_card"
        )
        waiting_status = next(
            status
            for status in waiting_statuses
            if status.device.device_id == enrolled_a.device.device_id
        )
        assert waiting_status.recovery_attempt == attempt
        assert waiting_status.recovery_attempt.state == "waiting"

        replacement_access = f"reissue-access-{uuid.uuid4().hex}"
        raw_secrets.add(replacement_access)

        async def prepare_reissue():
            return await store.profile_devices.prepare_reissue(
                attempt_id=attempt.attempt_id,
                registry_access_id="aut_card",
                card_revision=7,
                access_token=replacement_access,
                access_record={"operations": ["tools.call"]},
                token_response={
                    "access_token": replacement_access,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
                replacement_record={"scopes": ["tools:call"]},
                refresh_ttl_seconds=3600,
                access_ttl_seconds=3600,
            )

        first_reissue, second_reissue = await asyncio.gather(
            prepare_reissue(),
            prepare_reissue(),
        )
        assert sorted([first_reissue.replayed, second_reissue.replayed]) == [
            False,
            True,
        ]
        assert first_reissue.package == second_reissue.package
        reissue = first_reissue if not first_reissue.replayed else second_reissue

        fetched = await store.profile_devices.fetch_package(
            package_id=reissue.package.package_id,
            device_id=reissue.device.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
        )
        fetched_again = await store.profile_devices.fetch_package(
            package_id=reissue.package.package_id,
            device_id=reissue.device.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
        )
        assert fetched_again.ciphertext == fetched.ciphertext
        assert fetched.fetch_count == 1
        assert fetched_again.fetch_count == 2
        assert fetched.revision == reissue.package.revision + 1
        assert fetched_again.revision == fetched.revision + 1
        assert fetched.first_fetched_at is not None
        assert fetched_again.first_fetched_at == fetched.first_fetched_at
        assert fetched_again.last_fetched_at >= fetched.last_fetched_at
        with pytest.raises(DeviceAuthorityError, match="device_key_mismatch"):
            await store.profile_devices.fetch_package(
                package_id=reissue.package.package_id,
                device_id=reissue.device.device_id,
                device_thumbprint=key_b.thumbprint,
                registry_access_id="aut_card",
                card_revision=7,
            )
        payload = decrypt_device_package(
            fetched_again.ciphertext,
            private_jwk=key_a.private_jwk,
            expected_bindings=_package_bindings(fetched_again),
        )
        replacement_refresh = str(payload["token_response"]["refresh_token"])
        delivery_nonce = str(payload["delivery_nonce"])
        raw_secrets.update({replacement_refresh, delivery_nonce})
        delivered = await store.profile_devices.acknowledge_package(
            package_id=fetched.package_id,
            device_id=fetched.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
            delivery_nonce=delivery_nonce,
        )
        delivered_again = await store.profile_devices.acknowledge_package(
            package_id=fetched.package_id,
            device_id=fetched.device_id,
            device_thumbprint=key_a.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
            delivery_nonce=delivery_nonce,
        )
        assert delivered_again == delivered
        assert delivered.state == "delivered"
        with pytest.raises(DeviceAuthorityError, match="device_package_terminal"):
            await store.profile_devices.fetch_package(
                package_id=fetched.package_id,
                device_id=fetched.device_id,
                device_thumbprint=key_a.thumbprint,
                registry_access_id="aut_card",
                card_revision=8,
            )
        acknowledged_after_card_change = (
            await store.profile_devices.acknowledge_package(
                package_id=fetched.package_id,
                device_id=fetched.device_id,
                device_thumbprint=key_a.thumbprint,
                registry_access_id="aut_card",
                card_revision=8,
                delivery_nonce=delivery_nonce,
            )
        )
        assert acknowledged_after_card_change == delivered
        async with pool.acquire() as connection:
            delivered_family_state = await connection.fetchval(
                f"""
                SELECT state
                FROM {store.schema}.{TABLE_FAMILIES}
                WHERE family_id = $1
                """,
                delivered.family_id,
            )
        assert delivered_family_state == "active"
        with pytest.raises(DeviceAuthorityError, match="device_package_terminal"):
            await store.profile_devices.fetch_package(
                package_id=fetched.package_id,
                device_id=fetched.device_id,
                device_thumbprint=key_a.thumbprint,
                registry_access_id="aut_card",
                card_revision=7,
            )
        with pytest.raises(DeviceAuthorityError, match="device_package_nonce_mismatch"):
            await store.profile_devices.acknowledge_package(
                package_id=fetched.package_id,
                device_id=fetched.device_id,
                device_thumbprint=key_a.thumbprint,
                registry_access_id="aut_card",
                card_revision=7,
                delivery_nonce="different-delivery-nonce",
            )
        delivered_statuses = await store.profile_devices.list_card_device_statuses(
            "aut_card"
        )
        delivered_status = next(
            status
            for status in delivered_statuses
            if status.device.device_id == enrolled_a.device.device_id
        )
        assert delivered_status.package is not None
        assert delivered_status.package.state == "delivered"
        assert delivered_status.package.fetch_count == 2
        assert delivered_status.package.ciphertext == ""
        assert delivered_status.package.delivered_at is not None

        revoked = await store.profile_devices.revoke_device(
            enrolled_a.device.device_id
        )
        assert revoked.state == "revoked"
        async with pool.acquire() as connection:
            family_states = {
                str(row["family_id"]): str(row["state"])
                for row in await connection.fetch(
                    f"""
                    SELECT family_id, state
                    FROM {store.schema}.{TABLE_FAMILIES}
                    WHERE family_id = ANY($1::text[])
                    """,
                    [
                        enrolled_a.package.family_id,
                        reissue.package.family_id,
                        enrolled_b.package.family_id,
                    ],
                )
            }
            package_row = await connection.fetchrow(
                f"""
                SELECT state, jwe_ciphertext, delivery_nonce_sha256,
                       fetch_count, first_fetched_at, last_fetched_at
                FROM {store.schema}.{TABLE_DEVICE_PACKAGES}
                WHERE package_id = $1
                """,
                reissue.package.package_id,
            )
            records = await connection.fetch(
                f"""
                SELECT record::text AS value
                FROM {store.schema}.connection_hub_oauth_refresh_generations
                UNION ALL
                SELECT record::text AS value
                FROM {store.schema}.connection_hub_oauth_access_bindings
                """
            )
        assert family_states[enrolled_a.package.family_id] == "revoked"
        assert family_states[reissue.package.family_id] == "revoked"
        assert family_states[enrolled_b.package.family_id] == "active"
        assert package_row["state"] == "delivered"
        assert package_row["jwe_ciphertext"] is None
        assert package_row["fetch_count"] == 2
        assert package_row["first_fetched_at"] is not None
        assert package_row["last_fetched_at"] is not None
        assert package_row["delivery_nonce_sha256"] == bearer_sha256(
            delivery_nonce
        )
        serialized_records = "\n".join(str(row["value"]) for row in records)
        for secret in raw_secrets:
            assert secret not in serialized_records
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {store.schema} CASCADE"
            )
        await pool.close()

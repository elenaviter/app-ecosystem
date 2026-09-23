from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from connection_hub.delegated_credentials.devices import (
    DeviceAuthorityError,
    PostgresProfileDeviceAuthority,
    generate_device_key,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_DEVICE_ACCESS_BINDINGS,
    TABLE_DEVICE_FAMILIES,
    TABLE_DEVICE_PACKAGES,
    TABLE_PROFILE_DEVICES,
    TABLE_RECOVERY_ATTEMPTS,
    profile_device_authority_schema_sql,
)
class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.depth += 1
        self.connection.transaction_enters += 1

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.connection.transaction_exit_types.append(exc_type)
        self.connection.depth -= 1


class _Connection:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any] | None] | None = None,
        row_sets: list[list[dict[str, Any]]] | None = None,
        execute_results: list[str] | None = None,
    ) -> None:
        self.rows = deque(rows or [])
        self.row_sets = deque(row_sets or [])
        self.execute_results = deque(execute_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...], int]] = []
        self.depth = 0
        self.transaction_enters = 0
        self.transaction_exit_types: list[type[BaseException] | None] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args, self.depth))
        if self.execute_results:
            return self.execute_results.popleft()
        return "UPDATE 1" if sql.lstrip().startswith("UPDATE") else "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args, self.depth))
        return self.rows.popleft() if self.rows else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args, self.depth))
        return self.row_sets.popleft() if self.row_sets else []


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _authority(connection: _Connection) -> PostgresProfileDeviceAuthority:
    return PostgresProfileDeviceAuthority(
        pg_pool=_Pool(connection),
        schema="kdcube_demo_tenant_demo_project",
        tenant="demo-tenant",
        project="demo-project",
    )


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _device_row(key, *, revision: int = 1, family_id: str = "ofam_1") -> dict[str, Any]:
    return {
        "device_id": "odev_1",
        "registry_access_id": "aut_card",
        "card_kind": "automation",
        "enrollment_family_id": "ofam_1",
        "current_family_id": family_id,
        "client_id": "client-1",
        "subject": "user-1",
        "identity_scope": "grantor",
        "device_label": "Primary worker profile",
        "public_jwk": dict(key.public_jwk),
        "thumbprint": key.thumbprint,
        "enrolled_card_revision": 7,
        "revision": revision,
        "state": "active",
    }


def test_schema_is_additive_and_keeps_only_encrypted_package_material() -> None:
    sql = profile_device_authority_schema_sql("kdcube_demo_tenant_demo_project")

    for table in (
        TABLE_PROFILE_DEVICES,
        TABLE_DEVICE_FAMILIES,
        TABLE_DEVICE_ACCESS_BINDINGS,
        TABLE_RECOVERY_ATTEMPTS,
        TABLE_DEVICE_PACKAGES,
    ):
        assert f"CREATE TABLE IF NOT EXISTS kdcube_demo_tenant_demo_project.{table}" in sql
    assert "jwe_ciphertext" in sql
    assert "delivery_nonce_sha256" in sql
    assert "fetch_count" in sql
    assert "first_fetched_at" in sql
    assert "last_fetched_at" in sql
    assert "refresh_token " not in sql
    assert "access_token " not in sql
    assert "WHERE state IN ('waiting', 'ready')" in sql
    assert "WHERE state = 'current'" in sql
    assert "state <> 'ready' AND jwe_ciphertext IS NULL" in sql


@pytest.mark.asyncio
async def test_enrollment_rotates_and_persists_only_hashes_and_ciphertext(
    monkeypatch,
) -> None:
    key = generate_device_key()
    future = _future()
    family = {
        "family_id": "ofam_1",
        "registry_access_id": "aut_card",
        "card_kind": "automation",
        "client_id": "client-1",
        "subject": "user-1",
        "identity_scope": "grantor",
        "current_generation_id": "ogen_old",
        "state": "active",
        "expires_at": future,
    }
    generation = {
        "generation_id": "ogen_old",
        "family_id": "ofam_1",
        "record": {"client_id": "client-1", "sub": "user-1"},
        "state": "active",
        "expires_at": future,
    }
    package = {
        "package_id": "opkg_result",
        "package_kind": "enrollment",
        "attempt_id": None,
        "device_id": "odev_result",
        "device_revision": 1,
        "family_id": "ofam_1",
        "registry_access_id": "aut_card",
        "card_revision": 7,
        "state": "ready",
        "jwe_ciphertext": "sealed-result",
        "expires_at": future,
    }
    final_device = {
        **_device_row(key),
        "device_id": "odev_result",
    }
    connection = _Connection(
        rows=[
            {"family_id": "ofam_1"},
            None,
            generation,
            final_device,
            package,
        ],
        row_sets=[[family]],
    )
    generated = iter(["new-refresh-secret", "delivery-secret"])
    monkeypatch.setattr(
        "connection_hub.delegated_credentials.devices.enrollment_authority.secrets.token_urlsafe",
        lambda _size: next(generated),
    )

    transition = await _authority(connection).enroll_refresh_family(
        refresh_token="old-refresh-secret",
        expected_generation="ogen_old",
        public_jwk=key.public_jwk,
        device_label="Primary worker profile",
        registry_access_id="aut_card",
        card_kind="automation",
        card_revision=7,
        client_id="client-1",
        subject="user-1",
        identity_scope="grantor",
        replacement_record={"scopes": ["read"]},
        access_token="new-access-secret",
        access_record={"operations": ["search"]},
        token_response={
            "access_token": "new-access-secret",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
        refresh_ttl_seconds=3600,
        access_ttl_seconds=3600,
    )

    assert transition.replayed is False
    assert transition.device.device_id == "odev_result"
    assert connection.transaction_enters == 1
    assert connection.transaction_exit_types == [None]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    arguments = [
        argument
        for _kind, _sql, args, _depth in connection.calls
        for argument in args
    ]
    for raw_secret in (
        "old-refresh-secret",
        "new-refresh-secret",
        "new-access-secret",
        "delivery-secret",
    ):
        assert all(raw_secret not in str(argument) for argument in arguments)
    assert any(
        isinstance(argument, str) and argument.count(".") == 4
        for argument in arguments
    ), "the compact encrypted JWE is the only durable credential package"


@pytest.mark.asyncio
async def test_expiry_transition_commits_before_refusal() -> None:
    key = generate_device_key()
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    package = {
        "package_id": "opkg_1",
        "package_kind": "reissue",
        "attempt_id": "orcv_1",
        "device_id": "odev_1",
        "device_revision": 1,
        "family_id": "ofam_1",
        "registry_access_id": "aut_card",
        "card_revision": 7,
        "state": "ready",
        "jwe_ciphertext": "sealed",
        "delivery_nonce_sha256": "a" * 64,
        "expires_at": past,
    }
    connection = _Connection(
        rows=[
            {"device_id": "odev_1", "family_id": "ofam_1"},
            _device_row(key),
            package,
        ],
        row_sets=[[{"family_id": "ofam_1"}]],
    )

    with pytest.raises(DeviceAuthorityError, match="device_package_expired"):
        await _authority(connection).fetch_package(
            package_id="opkg_1",
            device_id="odev_1",
            device_thumbprint=key.thumbprint,
            registry_access_id="aut_card",
            card_revision=7,
        )

    assert connection.transaction_exit_types == [None]
    mutation_sql = "\n".join(
        sql for kind, sql, _args, _depth in connection.calls if kind == "execute"
    )
    assert "jwe_ciphertext = NULL" in mutation_sql
    assert "SET state = 'revoked'" in mutation_sql

@pytest.mark.asyncio
async def test_card_revision_change_commits_terminal_transition() -> None:
    key = generate_device_key()
    package = {
        "package_id": "opkg_1",
        "package_kind": "reissue",
        "attempt_id": "orcv_1",
        "device_id": "odev_1",
        "device_revision": 1,
        "family_id": "ofam_1",
        "registry_access_id": "aut_card",
        "card_revision": 7,
        "state": "ready",
        "jwe_ciphertext": "sealed",
        "delivery_nonce_sha256": "a" * 64,
        "expires_at": _future(),
    }
    connection = _Connection(
        rows=[
            {"device_id": "odev_1", "family_id": "ofam_1"},
            _device_row(key),
            package,
        ],
        row_sets=[[{"family_id": "ofam_1"}]],
    )

    with pytest.raises(
        DeviceAuthorityError,
        match="device_package_card_revision_changed",
    ):
        await _authority(connection).fetch_package(
            package_id="opkg_1",
            device_id="odev_1",
            device_thumbprint=key.thumbprint,
            registry_access_id="aut_card",
            card_revision=8,
        )

    assert connection.transaction_exit_types == [None]
    mutation_sql = "\n".join(
        sql for kind, sql, _args, _depth in connection.calls if kind == "execute"
    )
    assert "jwe_ciphertext = NULL" in mutation_sql
    assert "SET state = 'revoked'" in mutation_sql


@pytest.mark.asyncio
async def test_lost_terminal_transition_cannot_revoke_a_family() -> None:
    connection = _Connection(execute_results=["UPDATE 0"])

    await _authority(connection)._terminalize_package(
        connection,
        {
            "package_id": "opkg_delivered",
            "family_id": "ofam_live",
            "attempt_id": "orcv_delivered",
        },
        state="superseded",
        reason="card_revision_changed",
        invalidate_family=True,
    )

    assert len(connection.calls) == 1
    assert TABLE_DEVICE_PACKAGES in connection.calls[0][1]

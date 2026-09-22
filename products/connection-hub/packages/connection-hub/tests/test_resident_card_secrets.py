from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    CardHandleMetadataConflict,
    HANDLE_STATE_EXPIRED,
    HANDLE_STATE_REVOKED,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.resident_secrets import (
    RESIDENT_SECRET_MAX_BEARER_BYTES,
    RESIDENT_SECRET_SCHEMA,
    ResidentCardSecretService,
    ResidentSecretEnvelope,
    ResidentSecretError,
    resident_bearer_fingerprint,
)


ACCESS_ID = "agent_card_1"
BEARER = "kst1.resident-secret"
FINGERPRINT = resident_bearer_fingerprint(BEARER)
SECRET_REF_A = "a" * 32
SECRET_REF_B = "b" * 32


def _metadata(
    *,
    secret_ref: str = SECRET_REF_A,
    fingerprint: str = FINGERPRINT,
    state: str = "active",
    revision: int = 1,
    card_revision: int = 3,
    expires_at: int = 2_000,
) -> CardHandleMetadata:
    return CardHandleMetadata(
        access_id=ACCESS_ID,
        card_revision=card_revision,
        expires_at=expires_at,
        resident_access_secret_ref=secret_ref,
        resident_access_sha256=fingerprint,
        session_id="session-1",
        state=state,
        revision=revision,
        created_at=1_000,
        updated_at=1_000,
        retired_at=1_100 if state != "active" else 0,
    ).validated()


def _envelope(
    *,
    bearer: str = BEARER,
    card_revision: int = 3,
    expires_at: int = 2_000,
) -> str:
    return ResidentSecretEnvelope.create(
        access_id=ACCESS_ID,
        card_revision=card_revision,
        value=bearer,
        created_at=1_000,
        expires_at=expires_at,
    ).to_json()


class _SecretStore:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.events = events if events is not None else []
        self.create_error: Exception | None = None
        self.create_error_after_write = False
        self.get_errors: set[str] = set()
        self.delete_errors: set[str] = set()
        self.purge_error: Exception | None = None
        self.purge_result = 0

    async def create(self, *, secret_ref: str, value: str, expires_at: int) -> bool:
        self.events.append(f"secret.create:{secret_ref}:{expires_at}")
        if self.create_error is not None and not self.create_error_after_write:
            raise self.create_error
        if secret_ref in self.values:
            if self.create_error is not None:
                raise self.create_error
            return False
        self.values[secret_ref] = value
        if self.create_error is not None:
            raise self.create_error
        return True

    async def get(self, *, secret_ref: str) -> str | None:
        self.events.append(f"secret.get:{secret_ref}")
        if secret_ref in self.get_errors:
            raise RuntimeError("provider read failed")
        return self.values.get(secret_ref)

    async def delete(self, *, secret_ref: str) -> None:
        self.events.append(f"secret.delete:{secret_ref}")
        if secret_ref in self.delete_errors:
            raise RuntimeError("provider delete failed")
        self.values.pop(secret_ref, None)

    async def purge_expired(self, *, now: int, limit: int) -> int:
        self.events.append(f"secret.purge:{now}:{limit}")
        if self.purge_error is not None:
            raise self.purge_error
        return self.purge_result


class _MetadataStore:
    def __init__(
        self,
        current: CardHandleMetadata | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.current = current
        self.events = events if events is not None else []
        self.put_error: Exception | None = None
        self.clear_error: Exception | None = None
        self.clear_replacement: CardHandleMetadata | None = None
        self.retired: list[RetiredResidentSecret] = []
        self.terminal: list[CardHandleMetadata] = []
        self.expiring: list[CardHandleMetadata] = []

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        self.events.append(f"metadata.read:{access_id}")
        return self.current

    async def read_active(
        self, access_id: str, *, now: int | None = None
    ) -> CardHandleMetadata | None:
        current = await self.read_current(access_id)
        return current if current is not None and current.serves_at(now or 0) else None

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata:
        self.events.append(f"metadata.put:{expected_revision}")
        if self.put_error is not None:
            raise self.put_error
        old = self.current
        saved = replace(
            metadata.validated(),
            revision=expected_revision + 1,
            created_at=old.created_at if old is not None else 1_000,
            updated_at=1_001,
        )
        if (
            old is not None
            and old.resident_access_secret_ref
            and old.resident_access_secret_ref != saved.resident_access_secret_ref
        ):
            self.retired.append(
                RetiredResidentSecret(
                    access_id=old.access_id,
                    secret_ref=old.resident_access_secret_ref,
                    resident_access_sha256=old.resident_access_sha256,
                    retired_at=1_001,
                )
            )
        self.current = saved
        return saved

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> CardHandleMetadata | None:
        self.events.append(f"metadata.retire:{state}:{expected_revision}")
        if self.current is None:
            return None
        self.current = replace(
            self.current,
            state=state,
            revision=expected_revision + 1,
            retired_at=1_100,
        )
        return self.current

    async def expire_due(
        self, *, now: int | None = None, limit: int = 100
    ) -> list[CardHandleMetadata]:
        self.events.append(f"metadata.expire:{now}:{limit}")
        return list(self.expiring[:limit])

    async def clear_retired_resident_secret(
        self,
        access_id: str,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata | None:
        self.events.append(f"metadata.clear:{expected_revision}")
        if self.clear_error is not None:
            if self.clear_replacement is not None:
                self.current = self.clear_replacement
            raise self.clear_error
        if self.current is None:
            return None
        if self.current.revision != expected_revision:
            raise CardHandleMetadataConflict(
                "card_handle_revision_conflict",
                expected_revision=expected_revision,
                current_revision=self.current.revision,
            )
        self.current = replace(
            self.current,
            resident_access_secret_ref="",
            resident_access_sha256="",
            revision=expected_revision + 1,
        )
        return self.current

    async def list_secret_cleanup_candidates(
        self, *, limit: int = 100
    ) -> list[CardHandleMetadata]:
        self.events.append(f"metadata.list-terminal:{limit}")
        return list(self.terminal[:limit])

    async def list_retired_secret_cleanup_candidates(
        self, *, limit: int = 100
    ) -> list[RetiredResidentSecret]:
        self.events.append(f"metadata.list-retired:{limit}")
        return list(self.retired[:limit])

    async def delete_retired_secret_record(
        self, *, access_id: str, secret_ref: str
    ) -> bool:
        self.events.append(f"metadata.ack-retired:{access_id}:{secret_ref}")
        for index, candidate in enumerate(self.retired):
            if candidate.access_id == access_id and candidate.secret_ref == secret_ref:
                self.retired.pop(index)
                return True
        return False

    async def purge_terminal(self, *, retired_before: int, limit: int = 1_000) -> int:
        return 0


def _service(
    metadata: _MetadataStore,
    secrets: _SecretStore,
    *,
    reference: str = SECRET_REF_B,
) -> ResidentCardSecretService:
    return ResidentCardSecretService(
        metadata_store=metadata,
        secret_store=secrets,
        secret_ref_factory=lambda: reference,
    )


def test_resident_secret_envelope_is_bounded_strict_and_redacted() -> None:
    envelope = ResidentSecretEnvelope.create(
        access_id=ACCESS_ID,
        card_revision=3,
        value=BEARER,
        created_at=1_000,
        expires_at=2_000,
    )

    assert ResidentSecretEnvelope.from_json(envelope.to_json()) == envelope
    assert BEARER not in repr(envelope)
    assert json.loads(envelope.to_json())["schema"] == RESIDENT_SECRET_SCHEMA

    duplicate = envelope.to_json().replace(
        '"access_id":"agent_card_1",',
        '"access_id":"agent_card_1","access_id":"agent_card_1",',
    )
    with pytest.raises(ResidentSecretError) as duplicate_error:
        ResidentSecretEnvelope.from_json(duplicate)
    assert duplicate_error.value.reason == "resident_secret_envelope_duplicate_field"

    with pytest.raises(ResidentSecretError) as size_error:
        ResidentSecretEnvelope.create(
            access_id=ACCESS_ID,
            card_revision=3,
            value="x" * (RESIDENT_SECRET_MAX_BEARER_BYTES + 1),
            created_at=1_000,
            expires_at=2_000,
        )
    assert size_error.value.reason == "resident_secret_bearer_too_large"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "access_id",
            12345678901234567890123456789012,
            "resident_secret_access_id_invalid",
        ),
        ("card_revision", "3", "resident_secret_card_revision_invalid"),
        ("card_revision", 3.0, "resident_secret_card_revision_invalid"),
        ("created_at", True, "resident_secret_created_at_invalid"),
        ("expires_at", "2000", "resident_secret_expires_at_invalid"),
    ],
)
def test_resident_secret_envelope_rejects_alternate_json_field_types(
    field: str,
    value: Any,
    reason: str,
) -> None:
    record = json.loads(_envelope())
    record[field] = value

    with pytest.raises(ResidentSecretError) as error:
        ResidentSecretEnvelope.from_json(json.dumps(record))

    assert error.value.reason == reason


@pytest.mark.asyncio
async def test_install_writes_secret_before_metadata_and_resolves_without_writes() -> (
    None
):
    events: list[str] = []
    metadata = _MetadataStore(events=events)
    secrets = _SecretStore(events=events)
    service = _service(metadata, secrets)

    installed = await service.install(
        access_id=ACCESS_ID,
        card_revision=3,
        bearer=BEARER,
        session_id="session-1",
        expires_at=2_000,
        expected_revision=0,
        now=1_000,
    )

    assert installed.cleanup_failure is None
    assert installed.metadata.resident_access_secret_ref == SECRET_REF_B
    assert installed.metadata.resident_access_sha256 == FINGERPRINT
    assert events[:3] == [
        f"metadata.read:{ACCESS_ID}",
        f"secret.create:{SECRET_REF_B}:2000",
        "metadata.put:0",
    ]
    assert BEARER not in repr(installed.metadata)

    events.clear()
    assert await service.resolve(ACCESS_ID, now=1_500) == BEARER
    assert events == [
        f"metadata.read:{ACCESS_ID}",
        f"secret.get:{SECRET_REF_B}",
    ]


@pytest.mark.asyncio
async def test_install_retries_a_create_only_reference_collision_without_overwrite() -> (
    None
):
    events: list[str] = []
    metadata = _MetadataStore(events=events)
    secrets = _SecretStore(events=events)
    existing = ResidentSecretEnvelope.create(
        access_id="other_card_1",
        card_revision=1,
        value="kst1.existing",
        created_at=900,
        expires_at=3_000,
    ).to_json()
    secrets.values[SECRET_REF_A] = existing
    references = iter((SECRET_REF_A, SECRET_REF_B))
    service = ResidentCardSecretService(
        metadata_store=metadata,
        secret_store=secrets,
        secret_ref_factory=lambda: next(references),
    )

    installed = await service.install(
        access_id=ACCESS_ID,
        card_revision=3,
        bearer=BEARER,
        session_id="session-1",
        expires_at=2_000,
        expected_revision=0,
        now=1_000,
    )

    assert installed.metadata.resident_access_secret_ref == SECRET_REF_B
    assert secrets.values[SECRET_REF_A] == existing
    assert not any(event == f"secret.delete:{SECRET_REF_A}" for event in events)
    assert events[1:3] == [
        f"secret.create:{SECRET_REF_A}:2000",
        f"secret.create:{SECRET_REF_B}:2000",
    ]


@pytest.mark.asyncio
async def test_definitive_metadata_conflict_deletes_prepared_secret() -> None:
    events: list[str] = []
    metadata = _MetadataStore(_metadata(), events=events)
    metadata.put_error = CardHandleMetadataConflict(
        "card_handle_revision_conflict",
        expected_revision=1,
        current_revision=2,
    )
    secrets = _SecretStore(events=events)
    service = _service(metadata, secrets)

    with pytest.raises(CardHandleMetadataConflict):
        await service.install(
            access_id=ACCESS_ID,
            card_revision=4,
            bearer="kst1.replacement",
            session_id="session-2",
            expires_at=2_500,
            expected_revision=1,
            now=1_000,
        )

    assert SECRET_REF_B not in secrets.values
    assert events[-2:] == ["metadata.put:1", f"secret.delete:{SECRET_REF_B}"]


@pytest.mark.asyncio
async def test_outcome_unknown_metadata_error_keeps_expiring_prepared_secret() -> None:
    metadata = _MetadataStore(_metadata())
    metadata.put_error = OSError("connection lost while committing")
    secrets = _SecretStore()
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.install(
            access_id=ACCESS_ID,
            card_revision=4,
            bearer="kst1.replacement",
            session_id="session-2",
            expires_at=2_500,
            expected_revision=1,
            now=1_000,
        )

    assert error.value.reason == "resident_secret_metadata_commit_outcome_unknown"
    assert SECRET_REF_B in secrets.values
    assert not any(event.startswith("secret.delete") for event in secrets.events)


@pytest.mark.asyncio
async def test_secret_create_outcome_unknown_retains_a_possibly_created_record() -> (
    None
):
    metadata = _MetadataStore()
    secrets = _SecretStore()
    secrets.create_error = OSError("provider write outcome unknown")
    secrets.create_error_after_write = True
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.install(
            access_id=ACCESS_ID,
            card_revision=3,
            bearer=BEARER,
            session_id="session-1",
            expires_at=2_000,
            expected_revision=0,
            now=1_000,
        )

    assert error.value.reason == "resident_secret_create_outcome_unknown"
    assert secrets.events == [f"secret.create:{SECRET_REF_B}:2000"]
    assert SECRET_REF_B in secrets.values
    assert not any(event.startswith("metadata.put") for event in metadata.events)


@pytest.mark.asyncio
async def test_secret_create_outcome_unknown_never_deletes_an_existing_collision() -> (
    None
):
    metadata = _MetadataStore()
    secrets = _SecretStore()
    existing = ResidentSecretEnvelope.create(
        access_id="other_card_1",
        card_revision=1,
        value="kst1.existing",
        created_at=900,
        expires_at=3_000,
    ).to_json()
    secrets.values[SECRET_REF_B] = existing
    secrets.create_error = OSError("collision response lost")
    secrets.create_error_after_write = True
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.install(
            access_id=ACCESS_ID,
            card_revision=3,
            bearer=BEARER,
            session_id="session-1",
            expires_at=2_000,
            expected_revision=0,
            now=1_000,
        )

    assert error.value.reason == "resident_secret_create_outcome_unknown"
    assert secrets.values[SECRET_REF_B] == existing
    assert not any(event.startswith("secret.delete") for event in secrets.events)
    assert not any(event.startswith("metadata.put") for event in metadata.events)


@pytest.mark.asyncio
async def test_rotation_returns_visible_cleanup_failure_and_keeps_ledger() -> None:
    events: list[str] = []
    previous = _metadata()
    metadata = _MetadataStore(previous, events=events)
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = _envelope()
    secrets.delete_errors.add(SECRET_REF_A)
    service = _service(metadata, secrets)

    result = await service.install(
        access_id=ACCESS_ID,
        card_revision=4,
        bearer="kst1.replacement",
        session_id="session-2",
        expires_at=2_500,
        expected_revision=1,
        now=1_100,
    )

    assert result.metadata.resident_access_secret_ref == SECRET_REF_B
    assert result.cleanup_failure is not None
    assert result.cleanup_failure.reason == "resident_secret_cleanup_delete_failed"
    assert [item.secret_ref for item in metadata.retired] == [SECRET_REF_A]
    assert events.index(f"secret.delete:{SECRET_REF_A}") > events.index(
        "metadata.put:1"
    )
    assert not any(event.startswith("metadata.ack-retired") for event in events)


@pytest.mark.asyncio
async def test_resolution_fails_closed_on_envelope_metadata_mismatch() -> None:
    metadata = _MetadataStore(_metadata())
    secrets = _SecretStore()
    secrets.values[SECRET_REF_A] = _envelope(card_revision=2)
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.resolve(ACCESS_ID, now=1_500)

    assert error.value.reason == "resident_secret_card_revision_mismatch"
    assert BEARER not in str(error.value)


@pytest.mark.asyncio
async def test_retired_cleanup_deletes_verified_secret_before_acknowledgement() -> None:
    events: list[str] = []
    candidate = RetiredResidentSecret(
        access_id=ACCESS_ID,
        secret_ref=SECRET_REF_A,
        resident_access_sha256=FINGERPRINT,
        retired_at=1_100,
    )
    metadata = _MetadataStore(events=events)
    metadata.retired = [candidate]
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = _envelope()
    service = _service(metadata, secrets)

    result = await service.cleanup_retired_secrets(limit=10)

    assert result.scanned == result.completed == 1
    assert result.failures == ()
    assert events[-3:] == [
        f"secret.get:{SECRET_REF_A}",
        f"secret.delete:{SECRET_REF_A}",
        f"metadata.ack-retired:{ACCESS_ID}:{SECRET_REF_A}",
    ]
    assert metadata.retired == []


@pytest.mark.asyncio
async def test_cleanup_refuses_to_delete_a_mismatched_secret() -> None:
    events: list[str] = []
    candidate = RetiredResidentSecret(
        access_id=ACCESS_ID,
        secret_ref=SECRET_REF_A,
        resident_access_sha256=FINGERPRINT,
        retired_at=1_100,
    )
    metadata = _MetadataStore(events=events)
    metadata.retired = [candidate]
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = ResidentSecretEnvelope.create(
        access_id="other_card_1",
        card_revision=3,
        value=BEARER,
        created_at=1_000,
        expires_at=2_000,
    ).to_json()
    service = _service(metadata, secrets)

    result = await service.cleanup_retired_secrets()

    assert result.completed == 0
    assert result.failures[0].reason == "resident_secret_cleanup_access_id_mismatch"
    assert SECRET_REF_A in secrets.values
    assert not any(event.startswith("secret.delete") for event in events)
    assert not any(event.startswith("metadata.ack-retired") for event in events)


@pytest.mark.asyncio
async def test_retirement_commits_terminal_state_before_secret_cleanup() -> None:
    events: list[str] = []
    metadata = _MetadataStore(_metadata(), events=events)
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = _envelope()
    service = _service(metadata, secrets)

    result = await service.retire(
        ACCESS_ID,
        expected_revision=1,
        state=HANDLE_STATE_REVOKED,
    )

    assert result.cleanup_failure is None
    assert result.metadata is not None
    assert result.metadata.state == HANDLE_STATE_REVOKED
    assert result.metadata.resident_access_secret_ref == ""
    assert events.index("metadata.retire:revoked:1") < events.index(
        f"secret.delete:{SECRET_REF_A}"
    )
    assert events.index(f"secret.delete:{SECRET_REF_A}") < events.index(
        "metadata.clear:2"
    )


@pytest.mark.asyncio
async def test_terminal_cleanup_accepts_a_concurrent_fresh_reactivation() -> None:
    events: list[str] = []
    retired = _metadata(state=HANDLE_STATE_REVOKED, revision=2)
    replacement = _metadata(
        secret_ref=SECRET_REF_B,
        fingerprint=resident_bearer_fingerprint("kst1.replacement"),
        card_revision=4,
        revision=3,
    )
    metadata = _MetadataStore(retired, events=events)
    metadata.terminal = [retired]
    metadata.clear_replacement = replacement
    metadata.clear_error = CardHandleMetadataConflict(
        "card_handle_revision_conflict",
        expected_revision=2,
        current_revision=3,
    )
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = _envelope()
    service = _service(metadata, secrets)

    result = await service.cleanup_terminal_secrets()

    assert result.scanned == result.completed == 1
    assert result.failures == ()
    assert metadata.current == replacement
    assert SECRET_REF_A not in secrets.values


@pytest.mark.asyncio
async def test_expiry_cleanup_and_provider_purge_are_bounded() -> None:
    events: list[str] = []
    expired = _metadata(state=HANDLE_STATE_EXPIRED, revision=2)
    metadata = _MetadataStore(expired, events=events)
    metadata.expiring = [expired]
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = _envelope()
    secrets.purge_result = 3
    service = _service(metadata, secrets)

    result = await service.expire_due_and_cleanup(now=2_100, limit=5)
    purged = await service.purge_expired_secret_records(now=2_100, limit=7)

    assert result.scanned == result.completed == 1
    assert purged == 3
    assert "metadata.expire:2100:5" in events
    assert "secret.purge:2100:7" in events


def test_cards_package_exports_the_portable_custody_contract() -> None:
    from connection_hub.delegated_credentials import cards

    assert cards.ResidentCardSecretService is ResidentCardSecretService
    assert cards.ResidentSecretEnvelope is ResidentSecretEnvelope

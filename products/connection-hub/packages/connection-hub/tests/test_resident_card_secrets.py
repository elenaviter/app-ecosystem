from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from connection_hub.delegated_credentials.cards.handle_metadata import (
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
    HANDLE_STATE_EXPIRED,
    HANDLE_STATE_REVOKED,
    PREPARED_SECRET_STATE_CLEANUP,
    PREPARED_SECRET_STATE_INSTALLABLE,
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMutationResult,
    PreparedResidentSecret,
    ResidentSecretCleanupAcknowledgement,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.resident_secrets import (
    RESIDENT_SECRET_MAX_BEARER_BYTES,
    RESIDENT_SECRET_SCHEMA,
    ResidentCardSecretService,
    ResidentSecretCleanupService,
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


def _prepared(
    *,
    secret_ref: str = SECRET_REF_B,
    card_revision: int = 3,
    expires_at: int = 2_000,
) -> PreparedResidentSecret:
    return PreparedResidentSecret(
        access_id=ACCESS_ID,
        secret_ref=secret_ref,
        resident_access_sha256=FINGERPRINT,
        card_revision=card_revision,
        created_at=1_000,
        expires_at=expires_at,
    ).validated()


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
        self.read_error: Exception | None = None
        self.ack_error: Exception | None = None
        self.put_error: Exception | None = None
        self.clear_error: Exception | None = None
        self.clear_replacement: CardHandleMetadata | None = None
        self.prepared: dict[str, PreparedResidentSecret] = {}
        self.retired: list[RetiredResidentSecret] = []
        self.terminal: list[CardHandleMetadata] = []
        self.expiring: list[CardHandleMetadata] = []
        self.cleanup_claims: dict[
            tuple[str, str], ResidentSecretCleanupClaim
        ] = {}
        self.cleanup_attempts: dict[tuple[str, str], int] = {}
        self.cleanup_retry_after: dict[tuple[str, str], int] = {}

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        self.events.append(f"metadata.read:{access_id}")
        if self.read_error is not None:
            raise self.read_error
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
        saved, _retired = self._save(metadata, expected_revision=expected_revision)
        return saved

    def _save(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> tuple[CardHandleMetadata, RetiredResidentSecret | None]:
        if self.put_error is not None:
            raise self.put_error
        old = self.current
        saved = replace(
            metadata.validated(),
            revision=expected_revision + 1,
            created_at=old.created_at if old is not None else 1_000,
            updated_at=1_001,
        )
        retired = None
        if (
            old is not None
            and old.resident_access_secret_ref
            and old.resident_access_secret_ref != saved.resident_access_secret_ref
        ):
            retired = RetiredResidentSecret(
                access_id=old.access_id,
                secret_ref=old.resident_access_secret_ref,
                resident_access_sha256=old.resident_access_sha256,
                retired_at=1_001,
            )
            self.retired.append(retired)
        self.current = saved
        return saved, retired

    async def prepare_resident_secret(
        self,
        prepared: PreparedResidentSecret,
    ) -> bool:
        intent = prepared.validated()
        self.events.append(f"metadata.prepare:{intent.secret_ref}")
        unavailable = {
            item.secret_ref for item in self.retired
        } | set(self.prepared)
        if self.current is not None and self.current.resident_access_secret_ref:
            unavailable.add(self.current.resident_access_secret_ref)
        if intent.secret_ref in unavailable:
            return False
        self.prepared[intent.secret_ref] = intent
        return True

    async def install_prepared_resident_secret(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMutationResult:
        self.events.append(f"metadata.install:{expected_revision}")
        intent = self.prepared.get(metadata.resident_access_secret_ref)
        if intent is None:
            raise CardHandleMetadataConflict(
                "card_handle_prepared_secret_missing",
                expected_revision=expected_revision,
                current_revision=self.current.revision if self.current else 0,
            )
        if intent.state != PREPARED_SECRET_STATE_INSTALLABLE:
            raise CardHandleMetadataConflict(
                "card_handle_prepared_secret_cleanup_started",
                expected_revision=expected_revision,
                current_revision=self.current.revision if self.current else 0,
            )
        saved, retired = self._save(metadata, expected_revision=expected_revision)
        self.prepared.pop(intent.secret_ref, None)
        return CardHandleMutationResult(metadata=saved, retired_secret=retired)

    def _claim(
        self,
        *,
        source: str,
        access_id: str,
        secret_ref: str,
        fingerprint: str,
        now: int,
        card_revision: int = 0,
        created_at: int = 0,
        expires_at: int = 0,
        metadata_revision: int = 0,
    ) -> ResidentSecretCleanupClaim | None:
        key = (source, secret_ref)
        if key in self.cleanup_claims:
            return None
        if int(now) < self.cleanup_retry_after.get(key, 0):
            return None
        attempt = self.cleanup_attempts.get(key, 0) + 1
        self.cleanup_attempts[key] = attempt
        claim = ResidentSecretCleanupClaim(
            source=source,
            access_id=access_id,
            secret_ref=secret_ref,
            resident_access_sha256=fingerprint,
            claim_token=f"{len(self.cleanup_claims) + attempt:032x}",
            attempt=attempt,
            claimed_at=int(now),
            claim_expires_at=int(now) + 60,
            card_revision=card_revision,
            created_at=created_at,
            expires_at=expires_at,
            metadata_revision=metadata_revision,
        ).validated()
        self.cleanup_claims[key] = claim
        return claim

    async def claim_prepared_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: PreparedResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        self.events.append(f"metadata.claim-prepared:{limit}")
        records = [candidate.validated()] if candidate is not None else list(
            self.prepared.values()
        )
        current_ref = (
            self.current.resident_access_secret_ref if self.current is not None else ""
        )
        claims = []
        for record in records:
            durable = self.prepared.get(record.secret_ref)
            if durable is None:
                continue
            record = durable
            if record.secret_ref == current_ref:
                continue
            claim = self._claim(
                source=CLEANUP_SOURCE_PREPARED,
                access_id=record.access_id,
                secret_ref=record.secret_ref,
                fingerprint=record.resident_access_sha256,
                now=now,
                card_revision=record.card_revision,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
            if claim is not None:
                self.prepared[record.secret_ref] = replace(
                    record,
                    state=PREPARED_SECRET_STATE_CLEANUP,
                )
                claims.append(claim)
            if len(claims) >= limit:
                break
        return claims

    async def claim_retired_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: RetiredResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        self.events.append(f"metadata.claim-retired:{limit}")
        records = [candidate.validated()] if candidate is not None else list(self.retired)
        claims = []
        for record in records:
            claim = self._claim(
                source=CLEANUP_SOURCE_RETIRED,
                access_id=record.access_id,
                secret_ref=record.secret_ref,
                fingerprint=record.resident_access_sha256,
                now=now,
            )
            if claim is not None:
                claims.append(claim)
            if len(claims) >= limit:
                break
        return claims

    async def claim_terminal_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: CardHandleMetadata | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        self.events.append(f"metadata.claim-terminal:{limit}")
        records = [candidate.validated()] if candidate is not None else list(self.terminal)
        claims = []
        for record in records:
            if not record.resident_access_secret_ref:
                continue
            claim = self._claim(
                source=CLEANUP_SOURCE_TERMINAL,
                access_id=record.access_id,
                secret_ref=record.resident_access_secret_ref,
                fingerprint=record.resident_access_sha256,
                now=now,
                card_revision=record.card_revision,
                expires_at=record.expires_at,
                metadata_revision=record.revision,
            )
            if claim is not None:
                claims.append(claim)
            if len(claims) >= limit:
                break
        return claims

    async def acknowledge_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
    ) -> ResidentSecretCleanupAcknowledgement:
        item = claim.validated()
        if self.ack_error is not None:
            raise self.ack_error
        key = (item.source, item.secret_ref)
        if self.cleanup_claims.get(key) != item:
            return ResidentSecretCleanupAcknowledgement(acknowledged=False)
        if item.source == CLEANUP_SOURCE_PREPARED:
            self.cleanup_claims.pop(key, None)
            self.events.append(
                f"metadata.ack-prepared:{item.access_id}:{item.secret_ref}"
            )
            self.prepared.pop(item.secret_ref, None)
            return ResidentSecretCleanupAcknowledgement(acknowledged=True)
        if item.source == CLEANUP_SOURCE_RETIRED:
            self.cleanup_claims.pop(key, None)
            self.events.append(
                f"metadata.ack-retired:{item.access_id}:{item.secret_ref}"
            )
            self.retired = [
                record
                for record in self.retired
                if record.secret_ref != item.secret_ref
            ]
            return ResidentSecretCleanupAcknowledgement(acknowledged=True)
        self.events.append(f"metadata.ack-terminal:{item.access_id}:{item.secret_ref}")
        if self.clear_error is not None:
            if self.clear_replacement is not None:
                self.current = self.clear_replacement
            self.cleanup_claims.pop(key, None)
            return ResidentSecretCleanupAcknowledgement(acknowledged=False)
        if (
            self.current is None
            or self.current.revision != item.metadata_revision
            or self.current.resident_access_secret_ref != item.secret_ref
        ):
            return ResidentSecretCleanupAcknowledgement(acknowledged=False)
        self.cleanup_claims.pop(key, None)
        self.current = replace(
            self.current,
            resident_access_secret_ref="",
            resident_access_sha256="",
            revision=self.current.revision + 1,
        )
        return ResidentSecretCleanupAcknowledgement(
            acknowledged=True,
            metadata=self.current,
        )

    async def defer_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
        *,
        now: int,
        reason: str,
    ) -> bool:
        item = claim.validated()
        key = (item.source, item.secret_ref)
        if self.cleanup_claims.get(key) != item:
            return False
        self.events.append(
            f"metadata.defer:{item.source}:{item.secret_ref}:{reason}"
        )
        self.cleanup_claims.pop(key, None)
        self.cleanup_retry_after[key] = int(now) + 5
        return True

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
        f"metadata.prepare:{SECRET_REF_B}",
        f"secret.create:{SECRET_REF_B}:2000",
        "metadata.install:0",
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
    assert events[:7] == [
        f"metadata.prepare:{SECRET_REF_A}",
        f"secret.create:{SECRET_REF_A}:2000",
        "metadata.claim-prepared:1",
        (
            "metadata.defer:prepared:"
            f"{SECRET_REF_A}:resident_secret_reference_collision"
        ),
        f"metadata.prepare:{SECRET_REF_B}",
        f"secret.create:{SECRET_REF_B}:2000",
        "metadata.install:0",
    ]
    assert metadata.prepared[SECRET_REF_A].state == PREPARED_SECRET_STATE_CLEANUP
    assert metadata.cleanup_retry_after[(CLEANUP_SOURCE_PREPARED, SECRET_REF_A)] > 0


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
    assert events[-5:] == [
        "metadata.claim-prepared:1",
        f"metadata.read:{ACCESS_ID}",
        f"secret.get:{SECRET_REF_B}",
        f"secret.delete:{SECRET_REF_B}",
        f"metadata.ack-prepared:{ACCESS_ID}:{SECRET_REF_B}",
    ]
    assert metadata.prepared == {}


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
    assert list(metadata.prepared) == [SECRET_REF_B]
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
    assert list(metadata.prepared) == [SECRET_REF_B]
    assert not any(event.startswith("metadata.install") for event in metadata.events)


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
    assert list(metadata.prepared) == [SECRET_REF_B]
    assert not any(event.startswith("secret.delete") for event in secrets.events)
    assert not any(event.startswith("metadata.install") for event in metadata.events)


@pytest.mark.asyncio
async def test_prepared_cleanup_recovers_an_unadopted_created_secret() -> None:
    events: list[str] = []
    prepared = _prepared()
    metadata = _MetadataStore(events=events)
    metadata.prepared[prepared.secret_ref] = prepared
    secrets = _SecretStore(events=events)
    secrets.values[prepared.secret_ref] = _envelope()
    service = _service(metadata, secrets)

    result = await service.cleanup_prepared_secrets(limit=10)

    assert result.scanned == result.completed == 1
    assert result.failures == ()
    assert events[-5:] == [
        "metadata.claim-prepared:10",
        f"metadata.read:{ACCESS_ID}",
        f"secret.get:{SECRET_REF_B}",
        f"secret.delete:{SECRET_REF_B}",
        f"metadata.ack-prepared:{ACCESS_ID}:{SECRET_REF_B}",
    ]
    assert metadata.prepared == {}
    assert SECRET_REF_B not in secrets.values


@pytest.mark.asyncio
async def test_prepared_cleanup_refuses_a_different_envelope_creation_time() -> None:
    prepared = _prepared()
    metadata = _MetadataStore()
    metadata.prepared[prepared.secret_ref] = prepared
    secrets = _SecretStore()
    secrets.values[prepared.secret_ref] = ResidentSecretEnvelope.create(
        access_id=prepared.access_id,
        card_revision=prepared.card_revision,
        value=BEARER,
        created_at=prepared.created_at + 1,
        expires_at=prepared.expires_at,
    ).to_json()
    service = _service(metadata, secrets)

    result = await service.cleanup_prepared_secrets(limit=1)

    assert result.completed == 0
    assert result.failures[0].reason == (
        "resident_secret_cleanup_created_at_mismatch"
    )
    assert prepared.secret_ref in secrets.values
    assert prepared.secret_ref in metadata.prepared


@pytest.mark.asyncio
async def test_prepared_cleanup_never_deletes_a_current_reference() -> None:
    events: list[str] = []
    prepared = _prepared()
    metadata = _MetadataStore(
        _metadata(secret_ref=SECRET_REF_B),
        events=events,
    )
    metadata.prepared[prepared.secret_ref] = prepared
    secrets = _SecretStore(events=events)
    secrets.values[prepared.secret_ref] = _envelope()
    cleanup = ResidentSecretCleanupService(
        metadata_store=metadata,
        secret_store=secrets,
    )

    failure = await cleanup.cleanup_prepared_candidate(prepared)

    assert failure is not None
    assert failure.reason == "resident_secret_prepared_reference_is_current"
    assert SECRET_REF_B in secrets.values
    assert list(metadata.prepared) == [SECRET_REF_B]
    assert not any(event.startswith("secret.get") for event in events)
    assert not any(event.startswith("secret.delete") for event in events)


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
        "metadata.install:1"
    )
    assert not any(event.startswith("metadata.ack-retired") for event in events)


@pytest.mark.asyncio
async def test_stale_reference_for_superseded_card_revision_fails_closed() -> None:
    metadata = _MetadataStore(_metadata())
    secrets = _SecretStore()
    secrets.values[SECRET_REF_A] = _envelope(card_revision=2)
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.resolve(ACCESS_ID, now=1_500)

    assert error.value.reason == "resident_secret_card_revision_mismatch"
    assert BEARER not in str(error.value)


@pytest.mark.asyncio
async def test_stale_reference_reused_by_another_card_fails_closed() -> None:
    metadata = _MetadataStore(_metadata())
    secrets = _SecretStore()
    secrets.values[SECRET_REF_A] = ResidentSecretEnvelope.create(
        access_id="other_card_1",
        card_revision=1,
        value="kst1.other-card",
        created_at=1_200,
        expires_at=2_500,
    ).to_json()
    service = _service(metadata, secrets)

    with pytest.raises(ResidentSecretError) as error:
        await service.resolve(ACCESS_ID, now=1_500)

    assert error.value.reason == "resident_secret_access_id_mismatch"
    assert "kst1.other-card" not in str(error.value)


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
async def test_failed_cleanup_row_does_not_starve_the_next_due_row() -> None:
    events: list[str] = []
    first = RetiredResidentSecret(
        access_id=ACCESS_ID,
        secret_ref=SECRET_REF_A,
        resident_access_sha256=FINGERPRINT,
        retired_at=1_100,
    )
    second = RetiredResidentSecret(
        access_id=ACCESS_ID,
        secret_ref=SECRET_REF_B,
        resident_access_sha256=FINGERPRINT,
        retired_at=1_101,
    )
    metadata = _MetadataStore(events=events)
    metadata.retired = [first, second]
    secrets = _SecretStore(events=events)
    secrets.values[SECRET_REF_A] = ResidentSecretEnvelope.create(
        access_id="other_card_1",
        card_revision=3,
        value=BEARER,
        created_at=1_000,
        expires_at=2_000,
    ).to_json()
    secrets.values[SECRET_REF_B] = _envelope()
    service = _service(metadata, secrets)

    first_result = await service.cleanup_retired_secrets(limit=1)
    second_result = await service.cleanup_retired_secrets(limit=1)

    assert first_result.completed == 0
    assert first_result.failures[0].reason == (
        "resident_secret_cleanup_access_id_mismatch"
    )
    assert second_result.scanned == second_result.completed == 1
    assert second_result.failures == ()
    assert [item.secret_ref for item in metadata.retired] == [SECRET_REF_A]
    assert SECRET_REF_A in secrets.values
    assert SECRET_REF_B not in secrets.values


@pytest.mark.asyncio
async def test_deferred_prepared_cleanup_remains_non_installable() -> None:
    prepared = _prepared()
    metadata = _MetadataStore()
    metadata.prepared[prepared.secret_ref] = prepared

    claims = await metadata.claim_prepared_secret_cleanup(
        now=2_100,
        limit=1,
        candidate=prepared,
    )
    assert len(claims) == 1
    assert await metadata.defer_resident_secret_cleanup(
        claims[0],
        now=2_100,
        reason="resident_secret_cleanup_delete_failed",
    )

    with pytest.raises(CardHandleMetadataConflict) as error:
        await metadata.install_prepared_resident_secret(
            _metadata(
                secret_ref=prepared.secret_ref,
                fingerprint=prepared.resident_access_sha256,
                card_revision=prepared.card_revision,
                expires_at=prepared.expires_at,
            ),
            expected_revision=0,
        )

    assert error.value.reason == "card_handle_prepared_secret_cleanup_started"


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
        f"metadata.ack-terminal:{ACCESS_ID}:{SECRET_REF_A}"
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
async def test_terminal_cleanup_retries_when_ack_confirmation_is_unavailable() -> None:
    retired = _metadata(state=HANDLE_STATE_REVOKED, revision=2)
    metadata = _MetadataStore(retired)
    metadata.terminal = [retired]
    metadata.ack_error = RuntimeError("acknowledgement unavailable")
    metadata.read_error = RuntimeError("confirmation unavailable")
    secrets = _SecretStore()
    secrets.values[SECRET_REF_A] = _envelope()
    service = _service(metadata, secrets)

    result = await service.cleanup_terminal_secrets()

    assert result.scanned == 1
    assert result.completed == 0
    assert result.failures[0].reason == (
        "resident_secret_terminal_post_ack_read_failed"
    )
    assert SECRET_REF_A not in secrets.values
    assert metadata.cleanup_retry_after[(CLEANUP_SOURCE_TERMINAL, SECRET_REF_A)] > 0


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

    assert cards.CardHandleMutationResult is CardHandleMutationResult
    assert cards.PreparedResidentSecret is PreparedResidentSecret
    assert cards.ResidentSecretCleanupAcknowledgement is (
        ResidentSecretCleanupAcknowledgement
    )
    assert cards.ResidentSecretCleanupClaim is ResidentSecretCleanupClaim
    assert cards.RetiredResidentSecret is RetiredResidentSecret
    assert cards.ResidentCardSecretService is ResidentCardSecretService
    assert cards.ResidentSecretEnvelope is ResidentSecretEnvelope

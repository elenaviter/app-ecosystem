from __future__ import annotations

import fnmatch
import hashlib
import json

import pytest

from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_ADMISSION_REPLAY,
    FAMILY_CARD_HANDLES,
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    FAMILY_RESIDENT_CARD_SECRETS,
)
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AGENT,
    CARD_KIND_CONNECTOR,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_REVOKED,
    CardAuthority,
)
from connection_hub.delegated_credentials.migration.model import (
    build_migration_preview,
)
from connection_hub.delegated_credentials.migration.redis_source import (
    ConnectionHubRedisMigrationSource,
)
from connection_hub.delegated_credentials.migration.reset_source import (
    ConnectionHubRedisResetSource,
)
from connection_hub.delegated_credentials.migration.redis_scanner import (
    DurableRedisRecordError,
)


TENANT = "demo-tenant"
PROJECT = "demo-project"
EXPIRY_MS = 2_000_000_000_000


class _Redis:
    def __init__(self, records):
        self.records = dict(records)
        self.scan_calls: list[str] = []
        self.eval_calls: list[str] = []

    async def scan(self, *, cursor, match, count):
        self.scan_calls.append(match)
        return 0, [
            key.encode()
            for key in sorted(self.records)
            if fnmatch.fnmatch(key, match)
        ]

    async def eval(self, script, key_count, key):
        self.eval_calls.append(key)
        value, expiry = self.records[key]
        return [value.encode(), expiry]


class _Cards:
    def __init__(self, authority: CardAuthority | None):
        self.authority = authority

    async def load_card_authority(self, access_id: str):
        if self.authority is None or self.authority.access_id != access_id:
            return None
        return self.authority


class _CardsById:
    def __init__(self, authorities: list[CardAuthority]):
        self.authorities = {
            authority.access_id: authority for authority in authorities
        }

    async def load_card_authority(self, access_id: str):
        return self.authorities.get(access_id)


def _authority() -> CardAuthority:
    return CardAuthority(
        access_id="agent-card-1",
        client_id="client-1",
        grantor_subject="user-1",
        delegate_subject="agent-1",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        card_revision=3,
        created_at=1_900_000_000,
        expires_at=EXPIRY_MS // 1000,
    )


def _records():
    refresh = "refresh-secret"
    access_digest = hashlib.sha256(b"access-secret").hexdigest()
    admission_digest = hashlib.sha256(b"service\nnonce").hexdigest()
    return {
        f"{TENANT}:{PROJECT}:kdcube:oauth:client:dcr-client": (
            json.dumps(
                {
                    "client_id": "dcr-client",
                    "redirect_uris": ["https://client.example/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                    "metadata": {},
                }
            ),
            EXPIRY_MS,
        ),
        f"{TENANT}:{PROJECT}:kdcube:oauth:refresh:{refresh}": (
            json.dumps(
                {
                    "client_id": "dcr-client",
                    "sub": "user-1",
                    "registry_access_id": "agent-card-1",
                    "card_kind": CARD_KIND_AGENT,
                }
            ),
            EXPIRY_MS,
        ),
        f"{TENANT}:{PROJECT}:kdcube:oauth:agrant:{access_digest}": (
            json.dumps({"operations": ["read"]}),
            EXPIRY_MS,
        ),
        f"{TENANT}:{PROJECT}:kdcube:delegated-access:card-handles:agent-card-1": (
            json.dumps(
                {
                    "access_id": "agent-card-1",
                    "access_token": "resident-secret",
                    "session_id": "session-1",
                }
            ),
            EXPIRY_MS,
        ),
        f"connection-hub:admission:{TENANT}:{PROJECT}:nonce:{admission_digest}": (
            "1",
            EXPIRY_MS,
        ),
    }


@pytest.mark.asyncio
async def test_source_scans_every_durable_family_without_exposing_bearers() -> None:
    redis = _Redis(_records())
    inspection = await ConnectionHubRedisMigrationSource(
        redis,
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(_authority()),
    ).inspect(captured_at_ms=1_900_000_000_000)
    snapshot = inspection.snapshot

    assert snapshot.counts == {
        FAMILY_ADMISSION_REPLAY: 1,
        FAMILY_CARD_HANDLES: 1,
        FAMILY_OAUTH_ACCESS: 1,
        FAMILY_OAUTH_CLIENTS: 1,
        FAMILY_OAUTH_REFRESH: 1,
        FAMILY_RESIDENT_CARD_SECRETS: 1,
    }
    assert len(redis.scan_calls) == 7
    assert set(redis.eval_calls) == set(_records())
    preview = build_migration_preview(
        snapshot,
        generation_id="durable-authority-v1",
        prerequisites={"card_identity_cutover": {"reviewed": True}},
    )
    encoded = json.dumps(preview.to_dict(), sort_keys=True)
    assert "refresh-secret" not in encoded
    assert "resident-secret" not in encoded
    assert "user-1" not in encoded

    refresh = next(
        record for record in snapshot.records if record.record_type == "oauth_refresh"
    )
    assert refresh.secrets == {"bearer": "refresh-secret"}
    assert "refresh-secret" not in json.dumps(refresh.evidence())
    card = next(
        record for record in snapshot.records if record.record_type == "card_handles"
    )
    assert card.secrets == {"resident_bearer": "resident-secret"}
    assert card.payload["handles"] == {
        "access_id": "agent-card-1",
        "session_id": "session-1",
    }
    client = next(
        record for record in snapshot.records if record.record_type == "oauth_client"
    )
    assert client.payload["migration_state"] == "active"
    assert refresh.payload["migration_state"] == "active"
    assert inspection.source_summary["oauth_refresh_card_state"] == {
        "active": 1,
        "expired": 0,
        "missing": 0,
        "revoked": 0,
    }
    assert inspection.source_summary["legacy_card_rows"] == {
        "automation": 0,
        "control": 0,
    }
    assert inspection.blockers == ()


@pytest.mark.asyncio
async def test_reset_source_preserves_only_resident_card_credentials() -> None:
    redis = _Redis(_records())
    inspection = await ConnectionHubRedisResetSource(
        redis,
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(_authority()),
    ).inspect(captured_at_ms=1_900_000_000_000)

    assert inspection.snapshot.counts == {
        FAMILY_ADMISSION_REPLAY: 0,
        FAMILY_CARD_HANDLES: 1,
        FAMILY_OAUTH_ACCESS: 0,
        FAMILY_OAUTH_CLIENTS: 0,
        FAMILY_OAUTH_REFRESH: 0,
        FAMILY_RESIDENT_CARD_SECRETS: 1,
    }
    assert [record.record_type for record in inspection.snapshot.records] == [
        "card_handles"
    ]
    assert inspection.source_summary == {
        "preserved": {"resident_agent_card_handles": 1},
        "reset": {
            "admission_replay": 1,
            "legacy_automation_cards": 0,
            "legacy_control_cards": 0,
            "oauth_access": 1,
            "oauth_clients": 1,
            "oauth_refresh": 1,
        },
    }
    assert redis.eval_calls == [
        f"{TENANT}:{PROJECT}:kdcube:delegated-access:"
        "card-handles:agent-card-1"
    ]
    preview = build_migration_preview(
        inspection,
        generation_id="durable-authority-reset-v1",
        prerequisites={"reset_reconstructable_authority": True},
    )
    encoded = json.dumps(preview.to_dict(), sort_keys=True)
    assert "refresh-secret" not in encoded
    assert "resident-secret" not in encoded


@pytest.mark.asyncio
async def test_reset_source_does_not_read_discarded_handle_payloads() -> None:
    agent = _authority()
    connector = CardAuthority(
        access_id="connector-card-1",
        client_id="client-2",
        grantor_subject="user-1",
        delegate_subject="connector-1",
        source="connector",
        card_kind=CARD_KIND_CONNECTOR,
        card_revision=1,
        created_at=1_900_000_000,
        expires_at=EXPIRY_MS // 1000,
    )
    revoked_agent = CardAuthority(
        access_id="revoked-agent-card-1",
        client_id="client-3",
        grantor_subject="user-1",
        delegate_subject="agent-2",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        card_revision=1,
        created_at=1_800_000_000,
        expires_at=EXPIRY_MS // 1000,
        state=CARD_STATE_REVOKED,
    )
    expired_agent = CardAuthority(
        access_id="expired-agent-card-1",
        client_id="client-4",
        grantor_subject="user-1",
        delegate_subject="agent-3",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        card_revision=1,
        created_at=1_800_000_000,
        expires_at=1_850_000_000,
    )
    prefix = f"{TENANT}:{PROJECT}:kdcube:delegated-access:card-handles:"
    redis = _Redis(
        {
            prefix + agent.access_id: (
                json.dumps(
                    {
                        "access_id": agent.access_id,
                        "access_token": "resident-secret",
                        "session_id": "session-1",
                    }
                ),
                EXPIRY_MS,
            ),
            prefix + connector.access_id: ("not-json", EXPIRY_MS),
            prefix + revoked_agent.access_id: ("not-json", EXPIRY_MS),
            prefix + expired_agent.access_id: ("not-json", EXPIRY_MS),
            prefix + "orphan-card-1": ("not-json", EXPIRY_MS),
        }
    )

    inspection = await ConnectionHubRedisResetSource(
        redis,
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_CardsById(
            [agent, connector, revoked_agent, expired_agent]
        ),
    ).inspect(captured_at_ms=1_900_000_000_000)

    assert redis.eval_calls == [prefix + agent.access_id]
    assert inspection.source_summary["reset"] == {
        "admission_replay": 0,
        "card_handles_connector": 1,
        "card_handles_agent_expired": 1,
        "card_handles_agent_revoked": 1,
        "card_handles_orphaned": 1,
        "legacy_automation_cards": 0,
        "legacy_control_cards": 0,
        "oauth_access": 0,
        "oauth_clients": 0,
        "oauth_refresh": 0,
    }


@pytest.mark.asyncio
async def test_source_represents_persistent_unreferenced_client_as_retired() -> None:
    records = {
        f"{TENANT}:{PROJECT}:kdcube:oauth:client:unused-client": (
            json.dumps(
                {
                    "client_id": "unused-client",
                    "redirect_uris": ["https://client.example/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                    "metadata": {},
                }
            ),
            -1,
        )
    }

    snapshot = await ConnectionHubRedisMigrationSource(
        _Redis(records),
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(_authority()),
    ).snapshot(captured_at_ms=1_900_000_000_000)

    assert len(snapshot.records) == 1
    client = snapshot.records[0]
    assert client.expires_at_ms is None
    assert client.payload["migration_state"] == "retired"


@pytest.mark.asyncio
async def test_source_retires_refresh_whose_durable_card_is_missing() -> None:
    refresh_key = f"{TENANT}:{PROJECT}:kdcube:oauth:refresh:refresh-secret"
    snapshot = await ConnectionHubRedisMigrationSource(
        _Redis(
            {
                refresh_key: (
                    json.dumps(
                        {
                            "client_id": "dcr-client",
                            "registry_access_id": "missing-card",
                        }
                    ),
                    EXPIRY_MS,
                )
            }
        ),
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(None),
    ).inspect(captured_at_ms=1_900_000_000_000)

    refresh = snapshot.snapshot.records[0]
    assert refresh.payload["migration_state"] == "revoked"
    assert snapshot.source_summary["oauth_refresh_card_state"]["missing"] == 1


@pytest.mark.asyncio
async def test_source_blocks_unreconciled_legacy_card_rows() -> None:
    records = {
        f"{TENANT}:{PROJECT}:kdcube:delegated-access:control-card:control-1": (
            "{}",
            -1,
        ),
        f"{TENANT}:{PROJECT}:kdcube:delegated-access:automation:agent-1": (
            "{}",
            EXPIRY_MS,
        ),
    }
    inspection = await ConnectionHubRedisMigrationSource(
        _Redis(records),
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(None),
    ).inspect(captured_at_ms=1_900_000_000_000)

    assert inspection.source_summary["legacy_card_rows"] == {
        "automation": 1,
        "control": 1,
    }
    assert inspection.blockers == (
        "legacy_automation_card_reconciliation_required",
        "legacy_control_card_reconciliation_required",
    )


@pytest.mark.asyncio
async def test_source_error_never_exposes_a_bearer_key() -> None:
    bearer = "refresh-secret-that-must-not-appear"
    key = f"{TENANT}:{PROJECT}:kdcube:oauth:refresh:{bearer}"
    source = ConnectionHubRedisMigrationSource(
        _Redis({key: ("not-json", EXPIRY_MS)}),
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(None),
    )

    with pytest.raises(DurableRedisRecordError) as raised:
        await source.snapshot(captured_at_ms=1_900_000_000_000)

    assert bearer not in str(raised.value)
    assert raised.value.key_sha256 == hashlib.sha256(key.encode()).hexdigest()


@pytest.mark.asyncio
async def test_source_refuses_a_handle_without_its_durable_card_authority() -> None:
    records = {
        key: value
        for key, value in _records().items()
        if "card-handles" in key
    }
    source = ConnectionHubRedisMigrationSource(
        _Redis(records),
        tenant=TENANT,
        project=PROJECT,
        card_authorities=_Cards(None),
    )

    with pytest.raises(DurableRedisRecordError, match="authority_missing"):
        await source.snapshot(captured_at_ms=1_900_000_000_000)

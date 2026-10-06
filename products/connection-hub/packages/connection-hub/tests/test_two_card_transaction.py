"""Actual BundleStorage and fresh-process fixtures for two-Card transactions.

The transaction assertions will use Infra's typed callable and recovery reader.
This seed check only proves that the isolated harness persists two distinct
grantor partitions and that another Python process reads the same objects.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    subject_hash_for,
)


_CHILD = pathlib.Path(__file__).with_name("_two_card_subprocess.py")
_NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _authority(*, owner: str, access_id: str, issuer_kind: str) -> CardAuthority:
    return CardAuthority(
        access_id=access_id,
        client_id=f"synthetic:{access_id}",
        grantor_subject=owner,
        delegate_subject=f"synthetic-delegate:{access_id}",
        source="manual",
        card_kind=CARD_KIND_AUTOMATION,
        label=access_id,
        card_revision=1,
        catalog_version="synthetic-v1",
        resource_grants={"https://example.test/mcp": ("messages:read",)},
        resource_operations={"https://example.test/mcp": ("messages.search",)},
        named_service_operations=NamedServiceSelection.none(),
        created_at=int(_NOW.timestamp()),
        expires_at=int(_NOW.timestamp()) + 3600,
        issuer_kind=issuer_kind,
        issuer_ref="synthetic-issuer:pair-1",
        provenance={"synthetic_pair": "pair-1", "owner": owner},
    )


def _pair() -> tuple[CardAuthority, CardAuthority]:
    return (
        _authority(owner="synthetic-project", access_id="control_1", issuer_kind="project"),
        _authority(owner="synthetic-person", access_id="my_1", issuer_kind="project-person"),
    )


async def _seed_pair(store: BundleStorageDelegatedCardStore, pair: tuple[CardAuthority, CardAuthority]) -> None:
    for authority in pair:
        subject_hash = subject_hash_for(authority.grantor_subject)
        pointer = await store.write_revision(
            subject_hash=subject_hash, authority=authority, updated_at=_NOW
        )
        await store.advance_current(subject_hash=subject_hash, pointer=pointer)


def _run_child(request: dict) -> subprocess.CompletedProcess[str]:
    """Run a fresh interpreter; crash cases will assert its explicit exit code."""
    return subprocess.run(
        [sys.executable, str(_CHILD)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def _fresh_process_snapshot(root: pathlib.Path, pair: tuple[CardAuthority, CardAuthority]) -> dict:
    request = {
        "action": "snapshot",
        "storage_root": str(root),
        "targets": [
            {
                "subject_hash": subject_hash_for(authority.grantor_subject),
                "access_id": authority.access_id,
            }
            for authority in pair
        ],
    }
    child = _run_child(request)
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


@pytest.mark.asyncio
async def test_seeded_pair_is_durable_across_process_restart(tmp_path: pathlib.Path) -> None:
    pair = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path)
    await _seed_pair(store, pair)

    snapshot = _fresh_process_snapshot(tmp_path, pair)

    assert [card["access_id"] for card in snapshot["cards"]] == [
        authority.access_id for authority in pair
    ]
    assert [card["card_revision"] for card in snapshot["cards"]] == [1, 1]
    assert [card["content_hash"] for card in snapshot["cards"]] == [
        authority.content_hash() for authority in pair
    ]
    assert all(card["pointer_hash"] == card["content_hash"] for card in snapshot["cards"])

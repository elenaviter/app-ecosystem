"""Actual BundleStorage and fresh-process fixtures for two-Card transactions.

The transaction assertions will use Infra's typed callable and recovery reader.
This seed check only proves that the isolated harness persists two distinct
grantor partitions and that another Python process reads the same objects.
"""

from __future__ import annotations

import json
import pathlib
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.lifecycle import (
    LifecycleRefused,
    LifecycleRequest,
)
from connection_hub.delegated_credentials.cards import lifecycle_store
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    CardStorageError,
    subject_hash_for,
)
from connection_hub.delegated_credentials.durable_io import list_child_names
from connection_hub.delegated_credentials.issuer_gate import change_digest


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


def _request_body(pair: tuple[CardAuthority, CardAuthority]) -> dict:
    targets = [
        {
            "owner_subject": authority.grantor_subject,
            "access_id": authority.access_id,
            "expected_card_revision": authority.card_revision,
            "expected_authority_fingerprint": authority.content_hash(),
            "issuer_kind": authority.issuer_kind,
            "issuer_ref": authority.issuer_ref,
        }
        for authority in pair
    ]
    targets.sort(key=lambda target: (target["owner_subject"], target["access_id"]))
    return {
        "context_ref": "synthetic-context-1",
        "request_id": "synthetic-request-1",
        "action": "revoke",
        "change_digest": change_digest({"action": "revoke", "targets": targets}),
        "targets": targets,
    }


def _request_with_changed_target(body: dict, *, field: str, value: object) -> LifecycleRequest:
    changed = {**body, "targets": [dict(target) for target in body["targets"]]}
    changed["targets"][0][field] = value
    changed["targets"].sort(key=lambda target: (target["owner_subject"], target["access_id"]))
    changed["change_digest"] = change_digest(
        {"action": "revoke", "targets": changed["targets"]}
    )
    return LifecycleRequest.from_mapping(changed)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("access_id", "missing-id", "issuer_lifecycle_target_missing"),
        ("owner_subject", "wrong-owner", "issuer_lifecycle_target_missing"),
        ("expected_card_revision", 2, "issuer_lifecycle_revision_moved"),
        ("expected_authority_fingerprint", "f" * 64, "issuer_lifecycle_fingerprint_moved"),
        ("issuer_kind", "wrong-issuer", "issuer_lifecycle_target_binding_mismatch"),
        ("issuer_ref", "wrong-lineage", "issuer_lifecycle_target_binding_mismatch"),
    ],
)
async def test_recorded_target_mismatch_has_no_durable_effect(
    tmp_path: pathlib.Path, field: str, value: object, reason: str
) -> None:
    pair = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path)
    await _seed_pair(store, pair)
    request = _request_with_changed_target(_request_body(pair), field=field, value=value)
    actor = "authenticated-human"
    before = _fresh_process_snapshot(tmp_path, pair)
    revision_names = [
        await list_child_names(
            store.card_path(
                subject_hash=subject_hash_for(authority.grantor_subject),
                access_id=authority.access_id,
            ) / "revisions"
        )
        for authority in pair
    ]

    async def allow() -> None:
        pass

    with pytest.raises(LifecycleRefused, match=reason):
        await lifecycle_store.atomic_revoke(
            store, request=request, actor_subject=actor, now=_NOW, before_publish=allow
        )

    assert _fresh_process_snapshot(tmp_path, pair) == before
    assert await lifecycle_store.read_receipt(store, request.transaction_id(actor)) is None
    assert [
        await list_child_names(
            store.card_path(
                subject_hash=subject_hash_for(authority.grantor_subject),
                access_id=authority.access_id,
            ) / "revisions"
        )
        for authority in pair
    ] == revision_names


@pytest.mark.asyncio
async def test_committed_receipt_survives_killed_writer_and_replay_is_byte_stable(
    tmp_path: pathlib.Path,
) -> None:
    """Kill only a disposable writer after the shared commit rename, then retry elsewhere."""
    pair = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path)
    await _seed_pair(store, pair)
    body = _request_body(pair)
    request = LifecycleRequest.from_mapping(body)
    actor = "authenticated-human"
    child_request = {
        "storage_root": str(tmp_path),
        "body": body,
        "actor_subject": actor,
        "now": _NOW.isoformat(),
    }

    killed = _run_child({**child_request, "action": "fault_commit"})
    assert killed.returncode == -signal.SIGKILL, killed.stderr

    receipt_path = lifecycle_store.receipt_path(store, request.transaction_id(actor))
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["state"] == "committed"
    snapshot = _fresh_process_snapshot(tmp_path, pair)
    assert [(card["state"], card["card_revision"]) for card in snapshot["cards"]] == [
        ("revoked", 2),
        ("revoked", 2),
    ]

    replayed = _run_child({**child_request, "action": "replay"})
    assert replayed.returncode == 0, replayed.stderr
    assert json.loads(replayed.stdout) == receipt
    assert receipt_path.read_bytes() == receipt_bytes
    assert _fresh_process_snapshot(tmp_path, pair) == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_action", ["fault_prepare", "fault_first_pointer"])
async def test_prepared_intent_fences_an_unstaged_participant_after_writer_crash(
    tmp_path: pathlib.Path, fault_action: str,
) -> None:
    """A later single-Card writer cannot publish across a live pair intent."""
    pair = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path)
    await _seed_pair(store, pair)
    body = _request_body(pair)
    request = LifecycleRequest.from_mapping(body)
    actor = "authenticated-human"
    killed = _run_child(
        {
            "action": fault_action,
            "storage_root": str(tmp_path),
            "body": body,
            "actor_subject": actor,
            "now": _NOW.isoformat(),
        }
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr

    # The second target has no pending current pointer at either fault point.
    # A normal writer must see the active intent before publishing a new
    # current pointer, or recover the intent to a terminal refusal first.
    unstaged = next(card for card in pair if card.access_id == request.targets[1].access_id)
    other = replace(unstaged, card_revision=2, label="independent later mutation")
    try:
        pointer = await store.write_revision(
            subject_hash=subject_hash_for(other.grantor_subject),
            authority=other,
            updated_at=_NOW,
        )
        await store.advance_current(
            subject_hash=subject_hash_for(other.grantor_subject), pointer=pointer
        )
    except (CardStorageError, LifecycleRefused):
        pass

    reader = BundleStorageDelegatedCardStore(tmp_path)
    receipt = await lifecycle_store.read_receipt(reader, request.transaction_id(actor))
    assert receipt is not None
    if receipt["state"] == "prepared":
        observed = []
        for authority in pair:
            try:
                loaded = await reader.read_current_authority(
                    subject_hash=subject_hash_for(authority.grantor_subject),
                    access_id=authority.access_id,
                )
            except CardStorageError:
                observed.append("unavailable")
            else:
                assert loaded is not None
                observed.append((loaded[1].state, loaded[1].card_revision))
        assert observed in [
            [("active", 1), ("active", 1)],
            ["unavailable", "unavailable"],
        ]
    else:
        assert receipt["state"] == "refused"

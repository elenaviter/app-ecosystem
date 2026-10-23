"""W966: an outbox id in flight for several agents is settled in the copy its relay holds.

On dev-main (2026-10-23) one retirement publication, which every reporting
worker enqueues under the same id, sat leased in nine agents' folders. Settle
and retry looked the row up by id alone, found another agent's copy, met a
lease mismatch and skipped it (104 times an hour), so no copy ever left
flight. With a leased row always on disk, every channel's has_in_flight was
true on every turn and each turn claimed under the host-wide outbox lock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

PROJECT = "project-one"
A, B = "codex-a", "claude-b"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name in (A, B):
        store.register_worker(worker_name=name, runtime_kind="codex", capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT, title="Outbox", goal="Shared ids.", owner="operator")
    return store


def _leased(field, worker):
    return field.control / "projects" / PROJECT / "outbox" / worker / "leased"


def _shared_id_leased_by_both(field):
    """One row of A, then the same id in B's folder, each leased by its own relay."""

    row = field.enqueue_service_event(PROJECT, worker_name=A, kind="note.recorded", summary="shared",
                                      source_event_ref="local:w966:1")
    field.pull_outbox(relay_id="relay-a", worker_name=A)
    outbox_id = row["outbox_id"]
    copy = store_module.read_json(_leased(field, A) / f"{outbox_id}.json")
    copy.update(worker_name=B, lease={**copy["lease"], "relay_id": "relay-b"})
    _leased(field, B).mkdir(parents=True, exist_ok=True)
    store_module.atomic_write_json(_leased(field, B) / f"{outbox_id}.json", copy)
    return outbox_id


def test_each_relay_settles_its_own_copy_of_a_shared_id(field):
    outbox_id = _shared_id_leased_by_both(field)

    # Agent folders are walked in name order, so an id-only lookup finds B's
    # copy first: relay A, settling first, is the case that failed.
    field.settle_outbox(outbox_id, relay_id="relay-a", outcome="sent")
    assert not (_leased(field, A) / f"{outbox_id}.json").exists()
    assert store_module.read_json(_leased(field, B) / f"{outbox_id}.json")["lease"]["relay_id"] == "relay-b"

    field.settle_outbox(outbox_id, relay_id="relay-b", outcome="sent")
    assert not (_leased(field, B) / f"{outbox_id}.json").exists()


def test_each_relay_retries_its_own_copy_of_a_shared_id(field):
    outbox_id = _shared_id_leased_by_both(field)

    field.retry_outbox(outbox_id, relay_id="relay-a", error_code="e", error_summary="s")
    pending_a = field.control / "projects" / PROJECT / "outbox" / A / "pending" / f"{outbox_id}.json"
    assert pending_a.exists() and not (_leased(field, A) / f"{outbox_id}.json").exists()
    assert (_leased(field, B) / f"{outbox_id}.json").exists()


def test_a_release_returns_only_the_releasing_relays_copy(field):
    outbox_id = _shared_id_leased_by_both(field)

    assert field.release_outbox_claims([outbox_id], relay_id="relay-a") == [outbox_id]
    assert (field.control / "projects" / PROJECT / "outbox" / A / "pending" / f"{outbox_id}.json").exists()
    assert (_leased(field, B) / f"{outbox_id}.json").exists()


def test_a_relay_holding_no_copy_is_still_refused(field):
    outbox_id = _shared_id_leased_by_both(field)

    with pytest.raises(DomainError) as refused:
        field.settle_outbox(outbox_id, relay_id="relay-c", outcome="sent")
    assert refused.value.code == "field_outbox_lease_mismatch"
    assert (_leased(field, A) / f"{outbox_id}.json").exists() and (_leased(field, B) / f"{outbox_id}.json").exists()


def test_a_lookup_without_a_relay_keeps_the_first_copy(field):
    outbox_id = _shared_id_leased_by_both(field)

    path, folder = field._outbox.find_in_flight(outbox_id)
    assert folder == "leased" and path.name == f"{outbox_id}.json"

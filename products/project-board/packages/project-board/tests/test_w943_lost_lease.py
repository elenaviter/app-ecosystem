"""W943: a retry or settle of an outbox row whose lease is lost skips that row, never the channel.

On 2026-10-23 a retired-worker delivery the board kept refusing was retried by
the relay; by then the row's lease had passed to another claim, the retry
raised field_outbox_not_leased (non-retryable), and each such channel was torn
down and reconnected for 40-60 s. The lookup of the leased row also walked
every agent's settled history under the host-wide outbox lock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import outbox_store, relay
from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id=PROJECT, title="Outbox", goal="Lost leases.", owner="operator")
    return store


def _event(field: SharedFieldStore, index: int) -> dict:
    return field.enqueue_service_event(
        PROJECT, worker_name=WORKER, kind="note.recorded", summary=f"note {index}", source_event_ref=f"local:w943:{index}",
    )


def _expire_and_reclaim(field: SharedFieldStore, outbox_id: str, *, by: str = "relay-02") -> None:
    """The lease runs out and another claim takes the row, as on dev-main."""

    path = field.control / "projects" / PROJECT / "outbox" / WORKER / "leased" / f"{outbox_id}.json"
    row = store_module.read_json(path)
    row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    store_module.atomic_write_json(path, row)
    if by:
        assert [r["outbox_id"] for r in field.pull_outbox(relay_id=by, worker_name=WORKER)] == [outbox_id]


def _no_history(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a lease lookup walked the settled history")

    monkeypatch.setattr(outbox_store.OutboxStore, "partitioned", refuse)


def test_a_retry_after_another_claim_took_the_row_is_a_lease_mismatch_without_a_history_walk(field, monkeypatch):
    row = _event(field, 1)
    field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    _expire_and_reclaim(field, row["outbox_id"])
    _no_history(monkeypatch)

    with pytest.raises(DomainError) as lost:
        field.retry_outbox(row["outbox_id"], relay_id="relay-01", error_code="e", error_summary="s")
    assert lost.value.code == "field_outbox_lease_mismatch"


def test_a_row_back_in_pending_is_not_leased_and_the_history_is_not_read(field, monkeypatch):
    row = _event(field, 2)
    field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    _expire_and_reclaim(field, row["outbox_id"], by="")
    # Any claim returns an expired lease to pending first.
    field.pull_outbox(relay_id="relay-02", worker_name="someone-else")
    _no_history(monkeypatch)

    for call in (
        lambda: field.retry_outbox(row["outbox_id"], relay_id="relay-01", error_code="e", error_summary="s"),
        lambda: field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome="sent"),
    ):
        with pytest.raises(DomainError) as lost:
            call()
        assert lost.value.code == "field_outbox_not_leased"
        assert lost.value.details["state"] == "pending"


def test_a_settled_row_reads_as_not_in_flight(field, monkeypatch):
    row = _event(field, 3)
    field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome="sent")
    _no_history(monkeypatch)

    with pytest.raises(DomainError) as lost:
        field.settle_outbox(row["outbox_id"], relay_id="relay-01", outcome="sent")
    assert lost.value.code == "field_outbox_not_leased" and lost.value.details["state"] == "not_in_flight"


def _adapter(field: SharedFieldStore, client=None):
    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.field = field
    adapter.config = SimpleNamespace(relay_id="relay-01", worker_name=WORKER, project_id=PROJECT)
    adapter._outbox_finishes = set()
    adapter.client = client
    return adapter


def test_a_lost_lease_in_a_retry_or_settle_skips_the_row(field, caplog):
    row = _event(field, 4)
    field.pull_outbox(relay_id="relay-01", worker_name=WORKER)
    _expire_and_reclaim(field, row["outbox_id"])
    adapter = _adapter(field)

    async def finish_both():
        retried = await adapter._outbox_store(field.retry_outbox, finish=True)(
            row["outbox_id"], relay_id="relay-01", error_code="e", error_summary="s")
        settled = await adapter._outbox_store(field.settle_outbox, finish=True)(
            row["outbox_id"], relay_id="relay-01", outcome="sent")
        return retried, settled

    with caplog.at_level("WARNING"):
        assert asyncio.run(finish_both()) == (None, None)
    assert "lease is no longer this relay's" in caplog.text and row["outbox_id"] in caplog.text
    # The row stays with the claim that holds it.
    leased = field.control / "projects" / PROJECT / "outbox" / WORKER / "leased" / f"{row['outbox_id']}.json"
    assert store_module.read_json(leased)["lease"]["relay_id"] == "relay-02"


def test_other_refusals_and_claims_still_raise(field):
    adapter = _adapter(field)

    def refuses(*args, **kwargs):
        raise DomainError("field_outbox_lease_mismatch", "a claim, not a finish", status=409)

    def other(*args, **kwargs):
        raise DomainError("work_something_else", "another failure", status=409)

    with pytest.raises(DomainError):
        asyncio.run(adapter._outbox_store(refuses)("x"))
    with pytest.raises(DomainError):
        asyncio.run(adapter._outbox_store(other, finish=True)("x"))


def test_a_flush_whose_send_loses_the_lease_completes_and_the_channel_lives(field):
    row = _event(field, 5)

    class Board:
        async def action(self, **kwargs):
            # While the board answered slowly, the lease ran out and another claim took the row.
            _expire_and_reclaim(field, row["outbox_id"])
            raise DomainError("work_relay_transport_unavailable", "The Data Bus session is not connected.", status=503)

    counts = asyncio.run(_adapter(field, client=Board())._flush_outbox_unlocked())

    assert isinstance(counts, dict)

"""W819 (for W812 Option S): Hub per-operation counters for the board's measurement harness.

Counters only: without an observer nothing is recorded, and with one the operation's answer is unchanged.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from connection_hub import request_counters as rc
from test_project_person_access import PROJECT_REF, TARGET, _Host, _Port, _create, _lifecycle


@pytest.fixture
def snapshots():
    seen: list[dict] = []
    rc.set_observer(seen.append)
    try:
        yield seen
    finally:
        rc.set_observer(None)


def test_without_an_observer_nothing_is_recorded():
    rc.set_observer(None)
    with rc.operation_scope("card_version"):
        rc.count("secret_resolve")
        rc.phase("view", 5.0)
    # No scope was opened, so a count outside it has nowhere to go and raises nothing.
    rc.count("secret_resolve")


def test_one_snapshot_per_operation_with_actual_counts_and_no_identifiers(snapshots):
    with rc.operation_scope("card_version"):
        rc.count("participant_callers_build")
        for _ in range(4):
            rc.count("secret_resolve")
        rc.phase("read_authorize", 12.5)
        rc.count("not_a_counter")  # unknown names are ignored, never stored
    (snapshot,) = snapshots
    assert snapshot["surface"] == "hub" and snapshot["caller"] == "card_version"
    assert snapshot["counts"] == {"participant_callers_build": 1, "secret_resolve": 4, "read_authorize": 1}
    assert snapshot["phases_ms"] == {"read_authorize": 12.5}
    assert set(snapshot) == {"surface", "caller", "counts", "phases_ms", "wall_ms", "cpu_ms"}


def test_a_nested_scope_counts_into_the_outer_operation(snapshots):
    with rc.operation_scope("card_lifecycle_plan"):
        rc.count("secret_resolve")
        with rc.operation_scope("project_person_control_get"):
            rc.count("person_control_read")
    (snapshot,) = snapshots
    assert snapshot["caller"] == "card_lifecycle_plan"
    assert snapshot["counts"] == {"secret_resolve": 1, "person_control_read": 1}


def test_a_failing_observer_never_reaches_the_operation():
    def broken(_snapshot):
        raise RuntimeError("harness bug")
    rc.set_observer(broken)
    try:
        with rc.operation_scope("card_version"):
            rc.count("secret_resolve")
    finally:
        rc.set_observer(None)


def test_counted_keeps_the_operation_signature_and_result(snapshots):
    async def card_version(self, payload: dict, *, request: object = None) -> dict:
        rc.count("secret_resolve")
        return {"ok": True, "payload": payload}

    wrapped = rc.counted("card_version")(card_version)
    assert inspect.signature(wrapped) == inspect.signature(card_version)
    assert asyncio.run(wrapped(None, {"a": 1})) == {"ok": True, "payload": {"a": 1}}
    assert snapshots[0]["counts"] == {"secret_resolve": 1}


@pytest.mark.asyncio
async def test_the_person_control_read_answers_the_same_and_reports_its_phases():
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    rc.set_observer(None)
    plain = await _lifecycle(host, _Port()).get(
        actor_subject=TARGET, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-read")
    seen: list[dict] = []
    rc.set_observer(seen.append)
    try:
        counted = await _lifecycle(host, _Port()).get(
            actor_subject=TARGET, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-read")
    finally:
        rc.set_observer(None)
    assert counted == plain  # counters only: the answer is unchanged
    (snapshot,) = seen
    assert snapshot["caller"] == "project_person_control_get"
    assert snapshot["counts"]["person_control_read"] == 1
    assert {"read_authorize", "edit_authorize", "view"} <= set(snapshot["phases_ms"])
    assert all(ms >= 0 for ms in snapshot["phases_ms"].values())

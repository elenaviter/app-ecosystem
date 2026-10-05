"""W404: pb coordinate knows each operation's shape before it uses the relay.

Two failures on 2026-09-29 motivated this: a coordinator put project_ref in
the payload of project.plan.item instead of passing it as --object-ref, and a
retry after an applied review.accept reached the service as a different
request under the same key and was refused as an idempotency conflict.
"""

from __future__ import annotations

import argparse
import json

import pytest

from project_board.client import cli
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.client.coordinate_queue import MAX_COORDINATE_RESPONSE_BYTES
from project_board.client.coordinate_recovery import (
    CoordinateRecovery,
    coordinate_request_hash,
    error_outcome,
)
from project_board.contract.errors import DomainError
from project_board.contract.operation_shapes import (
    PROBLEM_BOARD_OPERATION_SHAPES,
    operation_contract,
)
from project_board.contract.worker_operation_contract import PROBLEM_BOARD_OPERATIONS

from relay_helpers import make_host

PROJECT = "work:project:quickstart-one"
WORK_REF = "work:plan:node:20260929T000000Z:w1:one"


def _args(action, *, object_ref="", payload=None, config="", contract=False, identity=None, timeout=30.0):
    return argparse.Namespace(
        action=action,
        object_ref=object_ref,
        payload_json=json.dumps(payload) if payload is not None else "",
        payload_file="",
        contract=contract,
        route="relay",
        timeout_seconds=timeout,
        runtime_kind=identity.runtime_kind if identity else "",
        runtime_session_id=identity.runtime_session_id if identity else "",
        config=config,
    )


@pytest.fixture
def submits(monkeypatch):
    calls: list[dict] = []
    original = CoordinateQueue.submit

    def counting_submit(self, **values):
        calls.append(values)
        return original(self, **values)

    monkeypatch.setattr(CoordinateQueue, "submit", counting_submit)
    return calls


def test_every_canonical_operation_has_a_shape():
    assert set(PROBLEM_BOARD_OPERATION_SHAPES) == set(PROBLEM_BOARD_OPERATIONS)
    for operation in PROBLEM_BOARD_OPERATIONS:
        contract = operation_contract(operation)
        assert contract["object_ref"], operation
        assert contract["example"].startswith(f"pb coordinate {operation} "), operation


def test_every_required_field_is_documented_in_its_payload():
    from project_board.contract.operation_shapes import PROBLEM_BOARD_OPERATION_REQUIRED

    for operation, fields in PROBLEM_BOARD_OPERATION_REQUIRED.items():
        payload = PROBLEM_BOARD_OPERATION_SHAPES[operation]["payload"]
        assert set(fields) <= set(payload), operation


def test_the_plan_item_contract_names_the_project_object_and_one_selector():
    result = cli._coordinate_command(_args("project.plan.item", contract=True))
    contract = result["contract"]
    assert contract["object_ref"] == ["work:project:<project_id>"]
    assert contract["one_of"] == [["item_key", "work_ref"]]
    assert contract["example"] == (
        "pb coordinate project.plan.item --object-ref <project-ref> "
        "--payload-json '{\"item_key\":\"<item_key>\"}'"
    )


def test_project_ref_in_the_payload_is_one_local_error_and_nothing_is_sent(submits, tmp_path):
    # codex-main, 2026-09-29: the project went into the payload.
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args(
                "project.plan.item",
                payload={"project_ref": PROJECT, "item_key": "W1"},
                config=str(tmp_path / "no-relay.json"),
            )
        )
    assert refused.value.code == "work_coordinate_shape_invalid"
    problems = refused.value.details["problems"]
    assert [problem["problem"] for problem in problems] == ["misplaced"]
    assert f"--object-ref {PROJECT}" in problems[0]["message"]
    assert refused.value.details["example"].startswith(
        "pb coordinate project.plan.item --object-ref <project-ref>"
    )
    assert "Nothing was sent." in str(refused.value)
    assert submits == []


@pytest.mark.parametrize(
    ("action", "payload", "missing"),
    [
        ("plan.item.create", {"idempotency_key": "k"}, "item"),
        # W404 review: the service requires the key for plan edits.
        ("plan.item.create", {"item": {"key": "W9"}}, "idempotency_key"),
        (
            "plan.item.update",
            {"work_ref": WORK_REF, "expected_revision": 3, "changes": {"title": "t"}},
            "idempotency_key",
        ),
        ("review.accept", {"work_ref": WORK_REF, "expected_revision": 3}, "idempotency_key"),
        (
            "review.return",
            {"work_ref": WORK_REF, "expected_revision": 3, "idempotency_key": "k"},
            "reason",
        ),
    ],
)
def test_a_missing_required_field_is_refused_locally(submits, tmp_path, action, payload, missing):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args(action, object_ref=PROJECT, payload=payload, config=str(tmp_path / "no-relay.json"))
        )
    assert refused.value.code == "work_coordinate_shape_invalid"
    assert [problem["field"] for problem in refused.value.details["problems"]] == [missing]
    assert submits == []


def test_an_unknown_operation_names_the_close_one(submits):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args("project.plan.items", object_ref=PROJECT, payload={}))
    assert refused.value.code == "work_coordinate_operation_unknown"
    assert "project.plan.item" in refused.value.details["close_matches"]
    assert submits == []


def _relay_completes(queue, channel, result):
    """Play the relay: claim the waiting request and answer it."""

    (request,) = queue.claim(worker_name=channel.worker_name)
    queue.complete(request, result=result)
    return request


def _receipt(state="applied"):
    return {
        "operation": "review.accept",
        "status": "ok",
        "state": state,
        "object": {"work_ref": WORK_REF, "status": "done"},
    }


def _w450_prior(host, channel, *, key="w450-recover", request_id="w450-prior"):
    """A disposable actor-owned request, never a live worker queue."""
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": key}
    recovery = CoordinateRecovery(host.field_root)
    request = {"action": "review.accept", "object_ref": PROJECT, "payload": payload}
    recovery.reserve(channel.worker_name, key, **request)
    queue = CoordinateQueue(host.field_root)
    queue.submit(
        worker_name=channel.worker_name, worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
        timeout_seconds=30, request_id=request_id, **request,
    )
    recovery.record_submission(channel.worker_name, key, request_id=request_id, **request)
    return payload, recovery, queue


@pytest.mark.parametrize("source", ["local_receipt", "late_relay_response"])
def test_w450_reconnecting_returns_exact_completed_receipt_without_a_new_request(
    monkeypatch, tmp_path, source
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    receipt = {"ok": True, "operation": "review.accept",
               "object": {"applied": True, "work_ref": WORK_REF, "status": "done"}}
    if source == "local_receipt":
        recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                                "w450-prior", "applied", receipt=receipt)
    else:
        _relay_completes(queue, channel, receipt)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})

    def forbidden(*args, **kwargs):
        pytest.fail("receipt recovery must not reserve, create an identity or submit")

    monkeypatch.setattr(CoordinateRecovery, "reserve", forbidden)
    monkeypatch.setattr(cli, "new_id", forbidden)
    monkeypatch.setattr(CoordinateQueue, "submit", forbidden)
    result = cli._coordinate_command(_args(
        "review.accept", object_ref=PROJECT, payload=payload,
        config=str(host.path), identity=identity,
    ))
    assert result["object"]["status"] == "done"
    assert result["recovery"]["source"] == source
    assert result["recovery"]["request_ids"] == ["w450-prior"]
    assert result["recovery"]["request_hash"] == coordinate_request_hash("review.accept", PROJECT, payload)


@pytest.mark.parametrize("changed", ["project", "work", "payload", "action", "key"])
def test_w450_reconnecting_never_reuses_a_receipt_for_a_different_request(
    monkeypatch, tmp_path, changed
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=_receipt())
    original = recovery.read(channel.worker_name, payload["idempotency_key"])
    action, project = "review.accept", PROJECT
    if changed == "project":
        project = "work:project:another"
    elif changed == "work":
        payload = dict(payload, work_ref="work:plan:node:20260929T000000Z:w2:two")
    elif changed == "payload":
        payload = dict(payload, expected_revision=8)
    elif changed == "action":
        action, payload = "review.return", dict(payload, reason="Return for changes")
    else:
        payload = dict(payload, idempotency_key="a-new-key")
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(cli, "new_id", lambda *a: pytest.fail("must not create a new identity"))
    monkeypatch.setattr(CoordinateQueue, "submit", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args(action, object_ref=project, payload=payload,
                                      config=str(host.path), identity=identity))
    expected = "work_coordinate_channel_reconnecting" if changed == "key" else "work_coordinate_idempotency_key_reused"
    assert refused.value.code == expected
    assert recovery.read(channel.worker_name, "w450-recover") == original
    assert recovery.read(channel.worker_name, "a-new-key") is None


@pytest.mark.parametrize("existing", [False, True])
def test_w450_reconnecting_unknown_or_new_request_creates_no_reservation(
    monkeypatch, tmp_path, existing
):
    host, identity, channel = make_host(tmp_path)
    if existing:
        payload, recovery, queue = _w450_prior(host, channel)
    else:
        payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "w450-recover"}
        recovery = CoordinateRecovery(host.field_root)
    original = recovery.read(channel.worker_name, payload["idempotency_key"])
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    for owner, name in [(CoordinateRecovery, "reserve"), (CoordinateRecovery, "_prune_unlocked"),
                        (CoordinateRecovery, "begin_attempt"), (CoordinateQueue, "submit"), (cli, "new_id")]:
        monkeypatch.setattr(owner, name, lambda *a, **k: pytest.fail("offline recovery must not allocate or prune"))
    with pytest.raises(DomainError) as blocked:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert blocked.value.code == "work_coordinate_channel_reconnecting"
    assert recovery.read(channel.worker_name, payload["idempotency_key"]) == original


@pytest.mark.parametrize("earlier_unknown", [False, True])
def test_w450_terminal_refusal_is_not_completion_of_an_earlier_unknown(
    monkeypatch, tmp_path, earlier_unknown
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    last = "w450-prior"
    if earlier_unknown:
        last = "w450-later"
        _w450_prior(host, channel, request_id=last)
    requests = {row["request_id"]: row for row in queue.claim(worker_name=channel.worker_name, limit=10)}
    queue.complete(requests[last], error={"code": "work_item_revision_conflict", "message": "changed", "status": 409})
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(cli, "new_id", lambda *a: pytest.fail("must not create a new identity"))
    monkeypatch.setattr(CoordinateQueue, "submit", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(DomainError) as raised:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    record = recovery.read(channel.worker_name, payload["idempotency_key"])
    if earlier_unknown:
        assert raised.value.code == "work_coordinate_channel_reconnecting"
        assert record["state"] == "outcome_unknown"
        assert record["attempts"] == {"w450-prior": "unknown", last: "refused"}
    else:
        assert raised.value.code == "work_item_revision_conflict"
        assert record is None, "all attempts proved no effect; the existing release semantics remain"


@pytest.mark.parametrize("invalid", ["missing", "wrong_operation", "refused", "mixed"])
def test_w450_cached_receipt_still_requires_the_original_operation_contract(
    monkeypatch, tmp_path, invalid
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    receipt = {"ok": True, "operation": "review.accept", "object": {"applied": True}}
    expected = "work_coordinate_response_invalid"
    if invalid == "missing":
        receipt = None
    elif invalid == "wrong_operation":
        receipt["operation"] = "plan.item.update"
    else:
        receipt["object"] = {"state": invalid}
        expected = "work_operation_mixed" if invalid == "mixed" else "work_operation_refused"
    recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=receipt)
    original = recovery.read(channel.worker_name, payload["idempotency_key"])
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(CoordinateQueue, "submit", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert refused.value.code == expected
    assert recovery.read(channel.worker_name, payload["idempotency_key"]) == original


def test_w450_another_actor_cannot_recover_the_same_key(monkeypatch, tmp_path):
    from project_board.client.host_config import enroll_worker_channel
    from project_board.contract.worker_identity import WorkerSessionIdentity

    host, identity, channel = make_host(tmp_path)
    other = WorkerSessionIdentity.create("codex", "22222222-2222-4222-8222-222222222222")
    other_channel = enroll_worker_channel(host.path, identity=other, profile="other-worker", authorized=True)
    payload, recovery, queue = _w450_prior(host, other_channel)
    recovery.settle_attempt(other_channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=_receipt())
    original = recovery.read(other_channel.worker_name, payload["idempotency_key"])
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(CoordinateQueue, "take_response", lambda *a, **k: pytest.fail("foreign queue must not be read"))
    monkeypatch.setattr(cli, "new_id", lambda *a: pytest.fail("must not create a new identity"))
    with pytest.raises(DomainError) as blocked:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert blocked.value.code == "work_coordinate_channel_reconnecting"
    assert "recovery" not in blocked.value.details
    assert recovery.read(channel.worker_name, payload["idempotency_key"]) is None
    assert recovery.read(other_channel.worker_name, payload["idempotency_key"]) == original


@pytest.mark.parametrize("gate", ["shape", "inactive", "missing", "timeout"])
def test_w450_front_door_checks_remain_before_local_receipt_access(monkeypatch, tmp_path, gate):
    from project_board.contract.worker_identity import WorkerSessionIdentity

    host, identity, channel = make_host(tmp_path, authorized=gate != "inactive")
    payload, recovery, queue = _w450_prior(host, channel)
    recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=_receipt())
    expected = "work_coordinate_shape_invalid"
    timeout = 30
    if gate == "shape":
        payload = {key: value for key, value in payload.items() if key != "expected_revision"}
    elif gate == "inactive":
        expected = "work_worker_channel_not_active"
    elif gate == "missing":
        identity = WorkerSessionIdentity.create("codex", "22222222-2222-4222-8222-222222222222")
        expected = "work_worker_channel_missing"
    else:
        timeout, expected = 0, "work_coordinate_timeout_invalid"
    monkeypatch.setattr(CoordinateRecovery, "lookup_existing", lambda *a, **k: pytest.fail("must not inspect a receipt"))
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity, timeout=timeout))
    assert refused.value.code == expected


def test_w450_expired_receipt_is_not_exposed_or_pruned_while_reconnecting(monkeypatch, tmp_path):
    import os
    import time
    from project_board.client.coordinate_recovery import RECOVERY_RETENTION_SECONDS

    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=_receipt())
    path = recovery._path(channel.worker_name, payload["idempotency_key"])
    stale = time.time() - RECOVERY_RETENTION_SECONDS - 60
    os.utime(path, (stale, stale))
    original = path.read_bytes()
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(CoordinateRecovery, "reserve", lambda *a, **k: pytest.fail("must not reserve"))
    with pytest.raises(DomainError) as blocked:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert blocked.value.code == "work_coordinate_channel_reconnecting"
    assert path.read_bytes() == original
    assert path.stat().st_mtime == stale


@pytest.mark.parametrize("change_payload", [False, True])
def test_w450_late_response_cannot_overwrite_a_concurrently_reused_key(
    monkeypatch, tmp_path, change_payload
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    _relay_completes(queue, channel, _receipt())
    take = CoordinateQueue.take_response
    replacement = {}

    def reuse_then_take(self, **values):
        if not replacement:
            # Deterministic interleaving after lookup, before late settlement.
            assert recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                                           "w450-prior", "refused") is None
            changed = dict(payload, expected_revision=8) if change_payload else payload
            request = {"action": "review.accept", "object_ref": PROJECT, "payload": changed}
            recovery.reserve(channel.worker_name, payload["idempotency_key"], **request)
            replacement.update(recovery.record_submission(
                channel.worker_name, payload["idempotency_key"], request_id="replacement", **request,
            ))
        return take(self, **values)

    monkeypatch.setattr(CoordinateQueue, "take_response", reuse_then_take)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(cli, "new_id", lambda *a: pytest.fail("must not create a new identity"))
    monkeypatch.setattr(CoordinateQueue, "submit", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    expected = "work_coordinate_idempotency_key_reused" if change_payload else "work_coordinate_channel_reconnecting"
    assert refused.value.code == expected
    assert recovery.read(channel.worker_name, payload["idempotency_key"]) == replacement
    assert replacement["attempts"] == {"replacement": "unknown"}
    assert "receipt" not in replacement


@pytest.mark.parametrize("invalid", ["wrong_operation", "mixed"])
def test_w450_invalid_late_receipt_remains_unknown_without_a_new_submission(
    monkeypatch, tmp_path, invalid
):
    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    receipt = {"ok": True, "operation": "review.accept", "object": {"applied": True}}
    if invalid == "wrong_operation":
        receipt["operation"] = "plan.item.update"
    else:
        receipt["object"] = {"state": "mixed", "summary": "Only part applied"}
    _relay_completes(queue, channel, receipt)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(cli, "new_id", lambda *a: pytest.fail("must not create a new identity"))
    monkeypatch.setattr(CoordinateQueue, "submit", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(DomainError) as blocked:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert blocked.value.code == "work_coordinate_channel_reconnecting"
    record = recovery.read(channel.worker_name, payload["idempotency_key"])
    assert record["state"] == "outcome_unknown"
    assert record["attempts"] == {"w450-prior": "unknown"}
    assert "receipt" not in record


@pytest.mark.parametrize("field", ["action", "object_ref", "payload"])
def test_w450_a_stored_hash_does_not_override_mismatched_request_fields(monkeypatch, tmp_path, field):
    from project_board.client.io import atomic_write_json

    host, identity, channel = make_host(tmp_path)
    payload, recovery, queue = _w450_prior(host, channel)
    recovery.settle_attempt(channel.worker_name, payload["idempotency_key"],
                            "w450-prior", "applied", receipt=_receipt())
    record = recovery.read(channel.worker_name, payload["idempotency_key"])
    record[field] = dict(payload, expected_revision=8) if field == "payload" else "different"
    atomic_write_json(recovery._path(channel.worker_name, payload["idempotency_key"]), record)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    monkeypatch.setattr(CoordinateQueue, "take_response", lambda *a, **k: pytest.fail("mismatched request must not read queue"))
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args("review.accept", object_ref=PROJECT, payload=payload,
                                      config=str(host.path), identity=identity))
    assert refused.value.code == "work_coordinate_idempotency_key_reused"
    assert recovery.read(channel.worker_name, payload["idempotency_key"]) == record


def test_an_applied_review_accept_is_recovered_without_a_second_request(submits, monkeypatch, tmp_path):
    # codex-main, 2026-09-29: the first wait ended before the receipt arrived.
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "require_successful_operation_envelope", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w1"}
    first = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError(
            "work_coordinate_outcome_unknown",
            "The relay claimed the governed operation but no result arrived before the deadline.",
            status=504,
            details={"worker_name": worker_name, "request_id": request_id},
        )

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    with pytest.raises(DomainError) as unknown:
        cli._coordinate_command(first)
    assert unknown.value.code == "work_coordinate_outcome_unknown"
    identity_shown = unknown.value.details["recovery"]
    assert identity_shown["idempotency_key"] == "accept-w1"
    assert identity_shown["request_hash"] == coordinate_request_hash("review.accept", PROJECT, payload)
    assert "unchanged" in unknown.value.details["retry"]
    assert len(submits) == 1

    # The relay applies it after the caller stopped waiting.
    _relay_completes(queue, channel, _receipt())
    monkeypatch.undo()
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "require_successful_operation_envelope", lambda *args, **kwargs: None)

    recovered = cli._coordinate_command(first)
    assert recovered["state"] == "applied"
    assert recovered["recovery"]["source"] == "late_relay_response"
    assert recovered["recovery"]["request_ids"] == [identity_shown["request_ids"][0]]
    assert len(submits) == 1, "the applied request is found, not sent again"

    again = cli._coordinate_command(first)
    assert again["recovery"]["source"] == "local_receipt"
    assert again["recovery"]["request_hash"] == identity_shown["request_hash"]
    assert len(submits) == 1

    changed = dict(payload, expected_revision=8)
    with pytest.raises(DomainError) as reused:
        cli._coordinate_command(
            _args("review.accept", object_ref=PROJECT, payload=changed, config=str(host.path), identity=identity)
        )
    assert reused.value.code == "work_coordinate_idempotency_key_reused"
    assert reused.value.details["original_request"]["payload"] == payload
    assert "Nothing was sent." in str(reused.value)
    assert len(submits) == 1


def test_an_outcome_still_unknown_is_resent_as_the_exact_same_request(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w2"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "unknown", status=504, details={})

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    for _ in range(2):
        with pytest.raises(DomainError):
            cli._coordinate_command(args)
    assert len(submits) == 2
    assert submits[0]["payload"] == submits[1]["payload"] == payload
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w2")
    assert record["state"] == "outcome_unknown"
    assert len(record["request_ids"]) == 2
    assert record["request_hash"] == coordinate_request_hash("review.accept", PROJECT, payload)


def test_a_refusal_frees_the_key_for_a_corrected_request(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w3"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def refused(queue_, path, *, worker_name, request_id, timeout_seconds):
        (request,) = queue.claim(worker_name=worker_name)
        queue.complete(
            request,
            error={"code": "work_item_revision_conflict", "message": "changed", "status": 409},
        )
        return queue.take_response(worker_name=worker_name, request_id=request_id)

    monkeypatch.setattr(cli, "_await_coordinate_response", refused)
    with pytest.raises(DomainError) as conflict:
        cli._coordinate_command(args)
    assert conflict.value.code == "work_item_revision_conflict"
    assert CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w3") is None


def test_the_worker_procedure_points_to_the_operation_contract():
    from pathlib import Path

    root = Path(cli.__file__).resolve().parents[1] / "procedures" / "problem-board-worker"
    # The skill points to the command-interface reference, which owns the
    # governed-operation path (W393); --contract lives there.
    skill = " ".join((root / "SKILL.md").read_text(encoding="utf-8").split())
    interface = " ".join((root / "references" / "pb-command-interface.md").read_text(encoding="utf-8").split())
    brief = " ".join((root / "references" / "brief-output.md").read_text(encoding="utf-8").split())
    assert "(references/pb-command-interface.md)" in skill
    assert "`pb coordinate <operation-id> --contract` prints the operation's object, payload fields and a copyable command from the operation catalog, and sends nothing" in interface
    assert "Every call is checked against that shape before it is sent" in interface
    assert "`work_coordinate_shape_invalid`, with the corrected command" in interface
    assert "`work_coordinate_idempotency_key_reused`" in interface
    assert "`work_coordinate_shape_invalid` and `work_coordinate_operation_unknown` come from the operation catalog before anything is sent" in brief
    assert "On `pb coordinate`, run the same command unchanged" in brief
    assert "`work_coordinate_idempotency_key_reused`, naming the original request" in brief
    assert "`work_coordinate_response_too_large` is in this class too" in brief
    assert "Every error that prints `recovery` and `retry` fields is in this class" in brief
    assert "A refusal of a later attempt says nothing about an earlier one" in brief


def test_two_calls_racing_with_one_key_cannot_both_be_sent(submits, monkeypatch, tmp_path):
    # W404 review: the key is held for one exact request before either call
    # can reach the relay, so a different request under it is refused.
    import threading

    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    first_in_submit = threading.Event()
    second_done = threading.Event()
    original_submit = CoordinateQueue.submit

    def slow_submit(self, **values):
        first_in_submit.set()
        second_done.wait(timeout=5)
        return original_submit(self, **values)  # the fixture's counting submit

    monkeypatch.setattr(CoordinateQueue, "submit", slow_submit)

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "unknown", status=504, details={})

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    first_payload = {"work_ref": WORK_REF, "expected_revision": 1, "idempotency_key": "race"}
    second_payload = dict(first_payload, expected_revision=2)
    outcomes: dict[str, BaseException] = {}

    def run(name, payload):
        try:
            cli._coordinate_command(
                _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
            )
        except BaseException as exc:  # noqa: BLE001
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("first", first_payload))
    first.start()
    assert first_in_submit.wait(timeout=5)
    run("second", second_payload)
    second_done.set()
    first.join(timeout=5)

    assert outcomes["second"].code == "work_coordinate_idempotency_key_reused"
    assert outcomes["first"].code == "work_coordinate_outcome_unknown"
    assert [call["payload"] for call in submits] == [first_payload]
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "race")
    assert record["payload"] == first_payload
    assert len(record["request_ids"]) == 1


def _expire_after_claim(queue, worker_name, request_id):
    """The queue's own expiry of a claimed request: the relay took it, then went quiet."""

    (request,) = queue.claim(worker_name=worker_name)
    assert request["request_id"] == request_id
    leased = queue._path("leased", worker_name, request_id)  # noqa: SLF001
    row = json.loads(leased.read_text(encoding="utf-8"))
    row["expires_at"] = "2020-01-01T00:00:00Z"
    row["lease_expires_at"] = "2020-01-01T00:00:00Z"
    leased.write_text(json.dumps(row), encoding="utf-8")
    assert queue.claim(worker_name=worker_name) == []


def _relay_fails_at_expiry(queue, worker_name, request_id):
    """The relay at expiry: data_bus_outcome_unknown goes through queue.fail."""

    (request,) = queue.claim(worker_name=worker_name)
    queue.fail(request, DomainError("data_bus_outcome_unknown", "no answer from the bus", status=504))


def _oversized_success(queue, worker_name, request_id):
    """An applied result too large for the queue is replaced by an error."""

    (request,) = queue.claim(worker_name=worker_name)
    queue.complete(request, result={"operation": "review.accept", "blob": "x" * (MAX_COORDINATE_RESPONSE_BYTES + 1)})


@pytest.mark.parametrize(
    ("relay", "code"),
    [
        (_expire_after_claim, "work_coordinate_outcome_unknown"),
        (_relay_fails_at_expiry, "data_bus_outcome_unknown"),
        (_oversized_success, "work_coordinate_response_too_large"),
    ],
)
def test_a_queued_uncertain_result_keeps_the_request_for_its_unchanged_retry(
    submits, monkeypatch, tmp_path, relay, code
):
    # W404 review return (codex-app, 2026-09-29): these three queued error
    # envelopes freed the key as if the service had refused, so a retry could
    # send a changed request while the first one may have applied.
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w9"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def relay_then_answer(queue_, path, *, worker_name, request_id, timeout_seconds):
        relay(queue, worker_name, request_id)
        response = queue.take_response(worker_name=worker_name, request_id=request_id)
        assert response is not None and response["ok"] is False
        return response

    monkeypatch.setattr(cli, "_await_coordinate_response", relay_then_answer)
    with pytest.raises(DomainError) as uncertain:
        cli._coordinate_command(args)
    assert uncertain.value.code == code
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w9")
    assert record is not None, "an uncertain result keeps the reservation"
    assert record["state"] == "outcome_unknown"
    assert record["payload"] == payload
    assert record["request_hash"] == coordinate_request_hash("review.accept", PROJECT, payload)
    assert len(record["request_ids"]) == 1
    shown = uncertain.value.details["recovery"]
    assert shown["idempotency_key"] == "accept-w9"
    assert shown["request_hash"] == record["request_hash"]
    assert shown["request_ids"] == record["request_ids"]
    assert "unchanged" in uncertain.value.details["retry"]

    changed = dict(payload, expected_revision=8)
    with pytest.raises(DomainError) as reused:
        cli._coordinate_command(
            _args("review.accept", object_ref=PROJECT, payload=changed, config=str(host.path), identity=identity)
        )
    assert reused.value.code == "work_coordinate_idempotency_key_reused"
    assert len(submits) == 1, "a changed request under the unresolved key is never sent"

    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    assert len(submits) == 2
    assert submits[1]["payload"] == payload, "the retry is the exact same request"


def test_an_unsent_retry_does_not_free_a_key_an_earlier_attempt_may_have_applied(
    submits, monkeypatch, tmp_path
):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w10"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
    errors = iter(
        [
            DomainError("work_coordinate_outcome_unknown", "unknown", status=504),
            DomainError("work_coordinate_relay_unavailable", "not claimed", status=504),
        ]
    )

    def answer(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise next(errors)

    monkeypatch.setattr(cli, "_await_coordinate_response", answer)
    for _ in range(2):
        with pytest.raises(DomainError):
            cli._coordinate_command(args)
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w10")
    assert record is not None and record["state"] == "outcome_unknown"
    assert len(record["request_ids"]) == 2


@pytest.mark.parametrize(
    ("code", "status", "outcome"),
    [
        ("work_coordinate_request_expired", 504, "not_sent"),
        ("work_coordinate_request_invalid", 400, "not_sent"),
        ("work_coordinate_request_too_large", 413, "not_sent"),
        ("work_coordinate_relay_unavailable", 504, "not_sent"),
        ("work_coordinate_channel_reconnecting", 503, "not_sent"),
        ("work_item_revision_conflict", 409, "refused"),
        ("work_review_operator_evidence_missing", 400, "refused"),
        ("work_coordinate_outcome_unknown", 504, "unknown"),
        ("work_coordinate_response_too_large", 502, "unknown"),
        ("work_coordinate_response_invalid", 502, "unknown"),
        ("work_coordinate_relay_failed", 502, "unknown"),
        ("data_bus_outcome_unknown", 504, "unknown"),
        ("data_bus_connect_refused", 503, "unknown"),
        ("work_store_unavailable", 503, "unknown"),
        ("", 400, "unknown"),
    ],
)
def test_only_proof_frees_a_key(code, status, outcome):
    assert error_outcome(DomainError(code, "message", status=status)) == outcome


# W404 second review return (codex-app, 2026-09-29): a later attempt's
# refusal or a late error cannot resolve an earlier attempt, and a receipt is
# final for its key.


def test_a_refused_retry_does_not_resolve_an_earlier_attempt_that_may_have_applied(
    submits, monkeypatch, tmp_path
):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w11"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
    answers = iter(["unknown", "card_not_active"])

    def answer(queue_, path, *, worker_name, request_id, timeout_seconds):
        if next(answers) == "unknown":
            raise DomainError("work_coordinate_outcome_unknown", "unknown", status=504)
        request = next(row for row in queue.claim(worker_name=worker_name, limit=10) if row["request_id"] == request_id)
        queue.complete(request, error={"code": "work_worker_card_not_active", "message": "denied", "status": 403})
        return queue.take_response(worker_name=worker_name, request_id=request_id)

    monkeypatch.setattr(cli, "_await_coordinate_response", answer)
    for code in ("work_coordinate_outcome_unknown", "work_worker_card_not_active"):
        with pytest.raises(DomainError) as raised:
            cli._coordinate_command(args)
        assert raised.value.code == code
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w11")
    assert record is not None, "the second attempt's refusal says nothing about the first"
    assert record["state"] == "outcome_unknown"
    first, second = record["request_ids"]
    assert record["attempts"] == {first: "unknown", second: "refused"}
    assert raised.value.details["recovery"]["attempts"] == record["attempts"]

    with pytest.raises(DomainError) as reused:
        cli._coordinate_command(
            _args("review.accept", object_ref=PROJECT, payload=dict(payload, expected_revision=8), config=str(host.path), identity=identity)
        )
    assert reused.value.code == "work_coordinate_idempotency_key_reused"
    assert len(submits) == 2, "a changed request is never sent under the unresolved key"


def test_a_receipt_is_final_for_its_key(tmp_path):
    recovery = CoordinateRecovery(tmp_path)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w12"}
    request = {"action": "review.accept", "object_ref": PROJECT, "payload": payload}
    recovery.reserve("w", "accept-w12", **request)
    recovery.record_submission("w", "accept-w12", request_id="r1", **request)
    recovery.record_submission("w", "accept-w12", request_id="r2", **request)
    recovery.settle_attempt("w", "accept-w12", "r2", "applied", receipt=_receipt())

    for outcome in ("unknown", "refused", "not_sent"):
        record = recovery.settle_attempt("w", "accept-w12", "r1", outcome)
        assert record is not None and record["state"] == "applied", outcome
        assert record["receipt"] == _receipt()
    late = recovery.record_submission("w", "accept-w12", request_id="r3", **request)
    assert late["state"] == "applied" and late["receipt"] == _receipt()


@pytest.mark.parametrize("reconnecting", [False, True])
def test_a_late_error_for_one_attempt_does_not_hide_another_attempts_receipt(
    submits, monkeypatch, tmp_path, reconnecting
):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "require_successful_operation_envelope", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w13"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "unknown", status=504)

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    for _ in range(2):
        with pytest.raises(DomainError):
            cli._coordinate_command(args)
    first, second = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w13")["request_ids"]

    # The first attempt applied; the second was then denied. Both answers are late.
    requests = {row["request_id"]: row for row in queue.claim(worker_name=channel.worker_name, limit=10)}
    queue.complete(requests[first], result=_receipt())
    queue.complete(requests[second], error={"code": "work_worker_unavailable", "message": "denied", "status": 403})

    if reconnecting:
        monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: {"state": "reconnecting"})
    recovered = cli._coordinate_command(args)
    assert recovered["state"] == "applied"
    assert recovered["recovery"]["source"] == "late_relay_response"
    assert len(submits) == 2, "the receipt is returned, nothing is sent again"
    record = CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w13")
    assert record["attempts"] == {first: "applied", second: "refused"}


def test_the_key_is_released_only_when_every_attempt_had_no_effect(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w14"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
    queue = CoordinateQueue(host.field_root)
    answers = iter(["unclaimed", "refused"])

    def answer(queue_, path, *, worker_name, request_id, timeout_seconds):
        if next(answers) == "unclaimed":
            queue.cancel_pending(worker_name=worker_name, request_id=request_id)
            raise DomainError("work_coordinate_relay_unavailable", "not claimed", status=504)
        (request,) = queue.claim(worker_name=worker_name)
        queue.complete(request, error={"code": "work_item_revision_conflict", "message": "changed", "status": 409})
        return queue.take_response(worker_name=worker_name, request_id=request_id)

    monkeypatch.setattr(cli, "_await_coordinate_response", answer)
    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    # Not sent and nothing else: released, so the same command starts over.
    assert CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w14") is None
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(args)
    assert refused.value.code == "work_item_revision_conflict"
    assert CoordinateRecovery(host.field_root).read(channel.worker_name, "accept-w14") is None
    assert len(submits) == 2


# W404 third review return (codex-app, 2026-09-29 21:41Z): a retry published
# into the queue before it was registered lost the key when an earlier
# attempt's refusal released it, and a changed payload was then sent.


def test_a_queued_retry_keeps_the_key_while_an_earlier_refusal_is_handled(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    recovery = CoordinateRecovery(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w15"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "unknown", status=504)

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    (first,) = recovery.read(channel.worker_name, "accept-w15")["request_ids"]
    (claimed,) = queue.claim(worker_name=channel.worker_name, limit=10)
    assert claimed["request_id"] == first

    # The unchanged retry is queued; before it is registered, attempt 1's
    # genuine refusal arrives and its normal handler settles it.
    queued = CoordinateQueue.submit

    def submit_then_first_refusal(self, **values):
        request = queued(self, **values)
        refusal = {"ok": False, "request_id": first, "error": {"code": "work_item_revision_conflict", "message": "changed", "status": 409}}
        with pytest.raises(DomainError):
            cli._finish_coordinate_response(refusal, recovery=recovery, worker_name=channel.worker_name, key="accept-w15", request_id=first)
        return request

    monkeypatch.setattr(CoordinateQueue, "submit", submit_then_first_refusal)
    with pytest.raises(DomainError) as retried:
        cli._coordinate_command(args)
    assert retried.value.code == "work_coordinate_outcome_unknown", "the retry was registered, not work_coordinate_recovery_missing"
    record = recovery.read(channel.worker_name, "accept-w15")
    assert record is not None and record["publishing"] == []
    second = record["request_ids"][1]
    assert record["attempts"] == {first: "refused", second: "unknown"}

    changed = _args("review.accept", object_ref=PROJECT, payload=dict(payload, expected_revision=8), config=str(host.path), identity=identity)
    with pytest.raises(DomainError) as reused:
        cli._coordinate_command(changed)
    assert reused.value.code == "work_coordinate_idempotency_key_reused"
    queued_payloads = [row["payload"] for row in queue.claim(worker_name=channel.worker_name, limit=10)]
    assert dict(payload, expected_revision=8) not in queued_payloads, "the changed request is never dispatched"


def test_a_definite_publication_failure_releases_only_an_unqueued_key(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    recovery = CoordinateRecovery(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w16"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
    queued = CoordinateQueue.submit
    failing = {"on": True}

    def submit(self, **values):
        if failing["on"]:
            raise DomainError("work_coordinate_request_too_large", "too large", status=413)
        return queued(self, **values)

    monkeypatch.setattr(CoordinateQueue, "submit", submit)
    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    assert recovery.read(channel.worker_name, "accept-w16") is None, "nothing was queued: the key is free"

    failing["on"] = False
    monkeypatch.setattr(cli, "_await_coordinate_response", lambda *a, **k: (_ for _ in ()).throw(DomainError("work_coordinate_outcome_unknown", "unknown", status=504)))
    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    failing["on"] = True
    with pytest.raises(DomainError):
        cli._coordinate_command(args)
    record = recovery.read(channel.worker_name, "accept-w16")
    assert record is not None and len(record["request_ids"]) == 1 and record["publishing"] == []


def test_a_publishing_attempt_is_never_released_by_a_concurrent_refusal(tmp_path):
    import threading

    recovery = CoordinateRecovery(tmp_path)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w17"}
    request = {"action": "review.accept", "object_ref": PROJECT, "payload": payload}
    missing: list[int] = []

    for round_number in range(40):
        key = f"accept-w17-{round_number}"
        body = dict(request, payload=dict(payload, idempotency_key=key))
        recovery.reserve("w", key, **body)
        recovery.record_submission("w", key, request_id="r1", **body)
        start = threading.Barrier(2)

        def publisher():
            start.wait()
            token = recovery.begin_attempt("w", key, request_id="r2", **body)
            try:
                recovery.record_submission("w", key, request_id="r2", token=token, **body)
            except DomainError:
                missing.append(round_number)

        def refuser():
            start.wait()
            recovery.settle_attempt("w", key, "r1", "refused")

        threads = [threading.Thread(target=publisher), threading.Thread(target=refuser)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        record = recovery.read("w", key)
        assert record is not None, round_number
        assert record["attempts"].get("r2") == "unknown"
    assert missing == []


# W404 fifth finding (codex-app, 2026-09-29 22:27Z): an interrupt after the
# queue published the request abandoned it as if it had never been sent.


def _sigint():
    """A real SIGINT delivered to this process, as the reviewer's probe sends it."""

    import signal

    signal.raise_signal(signal.SIGINT)
    raise AssertionError("SIGINT did not interrupt")


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit, RuntimeError, _sigint])
def test_an_interrupt_after_publication_keeps_the_key(submits, monkeypatch, tmp_path, interrupt):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    queue = CoordinateQueue(host.field_root)
    recovery = CoordinateRecovery(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w18"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
    published = CoordinateQueue.submit

    def publish_then_interrupt(self, **values):
        published(self, **values)
        raise interrupt()

    monkeypatch.setattr(CoordinateQueue, "submit", publish_then_interrupt)
    expected = KeyboardInterrupt if interrupt is _sigint else interrupt
    with pytest.raises(expected):
        cli._coordinate_command(args)
    (claimed,) = queue.claim(worker_name=channel.worker_name, limit=10)
    record = recovery.read(channel.worker_name, "accept-w18")
    assert record is not None, "the published request may apply: the key stays held"
    assert record["request_ids"] == [claimed["request_id"]]
    assert record["attempts"] == {claimed["request_id"]: "unknown"} and record["publishing"] == []

    monkeypatch.setattr(CoordinateQueue, "submit", published)
    with pytest.raises(DomainError) as reused:
        cli._coordinate_command(
            _args("review.accept", object_ref=PROJECT, payload=dict(payload, expected_revision=8), config=str(host.path), identity=identity)
        )
    assert reused.value.code == "work_coordinate_idempotency_key_reused"
    assert len(submits) == 1, "the changed request is never dispatched"


def test_an_interrupt_before_publication_frees_the_key_and_an_unreadable_queue_holds_it(submits, monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    recovery = CoordinateRecovery(host.field_root)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": "accept-w19"}
    args = _args("review.accept", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)

    def interrupt_first(self, **values):
        raise KeyboardInterrupt()

    monkeypatch.setattr(CoordinateQueue, "submit", interrupt_first)
    with pytest.raises(KeyboardInterrupt):
        cli._coordinate_command(args)
    assert recovery.read(channel.worker_name, "accept-w19") is None, "never published: the key is free"

    def unreadable(self, **values):
        raise OSError("queue not readable")

    monkeypatch.setattr(CoordinateQueue, "holds", unreadable)
    with pytest.raises(KeyboardInterrupt):
        cli._coordinate_command(args)
    record = recovery.read(channel.worker_name, "accept-w19")
    assert record is not None and len(record["publishing"]) == 1, "unknown publication stays held"


# W404 sixth finding (codex-app, 2026-09-29 22:44Z): the presence scan read
# pending, leased and responses without the queue lock, so a relay requeue
# between the reads made a published request look absent.


def test_the_presence_scan_is_consistent_with_a_concurrent_requeue(tmp_path, monkeypatch):
    import threading
    import time

    host, identity, channel = make_host(tmp_path)
    queue = CoordinateQueue(host.field_root)
    queue.submit(
        worker_name=channel.worker_name, worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
        action="review.accept", object_ref=PROJECT, payload={"idempotency_key": "k"}, request_id="coordinate_req1",
    )
    (claimed,) = queue.claim(worker_name=channel.worker_name, limit=10)
    requeued = threading.Event()

    def requeue():
        queue.defer_after_unknown(claimed, error=DomainError("data_bus_outcome_unknown", "unknown", status=504))
        requeued.set()

    real_path = CoordinateQueue._path
    started = {"thread": None}

    def path_that_lets_the_relay_move(self, state, worker_name, request_id):
        if state == "leased" and request_id == "coordinate_req1" and started["thread"] is None:
            # Pending has been read (absent: the request is claimed). Before
            # leased is read, the relay tries to requeue it (leased -> pending).
            started["thread"] = threading.Thread(target=requeue)
            started["thread"].start()
            time.sleep(0.3)
        return real_path(self, state, worker_name, request_id)

    monkeypatch.setattr(CoordinateQueue, "_path", path_that_lets_the_relay_move)
    assert queue.holds(worker_name=channel.worker_name, request_id="coordinate_req1") is True
    started["thread"].join(timeout=5)
    assert requeued.is_set(), "the requeue ran after the scan released the lock"
    monkeypatch.setattr(CoordinateQueue, "_path", real_path)
    assert queue.holds(worker_name=channel.worker_name, request_id="coordinate_req1") is True
    assert queue.holds(worker_name=channel.worker_name, request_id="coordinate_absent") is False


def test_an_unreadable_queue_location_is_not_absence(tmp_path, monkeypatch):
    # Pins behaviour the earlier scan already had; kept so the strict
    # FileNotFoundError-only absence cannot regress.
    import os

    host, identity, channel = make_host(tmp_path)
    queue = CoordinateQueue(host.field_root)
    real_stat = os.stat

    def denied(path, *args, **kwargs):
        if "coordinate_req2" in str(path):
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    with pytest.raises(PermissionError):
        queue.holds(worker_name=channel.worker_name, request_id="coordinate_req2")


# W404 ownership 8, 2026-09-30: root sent plan.item.update with
# changes["review.look_at"]. The contract said only "supported item fields",
# the call reached the service, and it was refused work_item_changes_invalid.
# The service reads changes.review as a nested object.


def _update(changes, *, key="w404-nested-1"):
    return {"work_ref": WORK_REF, "expected_revision": 41, "changes": changes, "idempotency_key": key}


def test_the_update_contract_names_every_item_field_and_the_nested_review():
    from project_board.contract.operation_shapes import PLAN_ITEM_CHANGE_FIELDS

    contract = cli._coordinate_command(_args("plan.item.update", contract=True))["contract"]
    changes = contract["payload"]["changes"]
    assert changes == PLAN_ITEM_CHANGE_FIELDS
    assert set(changes["review"]) == {"look_at", "could_not_verify", "hold"}
    assert "status" not in changes, "lifecycle status has its own operations"

    example = json.loads(contract["example"].split("--payload-json ", 1)[1].strip("'"))
    assert isinstance(example["expected_revision"], int)
    assert isinstance(example["changes"]["review"], dict)
    assert example["changes"]["review"]["could_not_verify"] == "None"


def test_a_dotted_review_field_is_one_local_error_with_the_nested_form_and_nothing_is_sent(submits, tmp_path):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args(
                "plan.item.update",
                object_ref=PROJECT,
                payload=_update({"review.look_at": "Run the suite.", "review.could_not_verify": "None", "title": "T"}),
                config=str(tmp_path / "no-relay.json"),
            )
        )
    assert refused.value.code == "work_coordinate_shape_invalid"
    [problem] = refused.value.details["problems"]
    assert problem["problem"] == "dotted"
    assert problem["field"] == "changes.review.could_not_verify, changes.review.look_at"
    assert '{"review":{"look_at":"<look_at>","could_not_verify":"<could_not_verify>"}}' in problem["message"]
    assert json.loads(problem["corrected"]) == {
        "review": {"look_at": "Run the suite.", "could_not_verify": "None"},
        "title": "T",
    }
    assert "Nothing was sent." in str(refused.value)
    assert submits == []


@pytest.mark.parametrize(
    ("changes", "field"),
    [("title=T", "changes"), ({"review": "Run the suite."}, "changes.review")],
)
def test_a_changes_value_of_the_wrong_type_is_refused_locally(submits, tmp_path, changes, field):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args("plan.item.update", object_ref=PROJECT, payload=_update(changes), config=str(tmp_path / "no-relay.json"))
        )
    assert [(p["field"], p["problem"]) for p in refused.value.details["problems"]] == [(field, "type")]
    assert submits == []


# W537: the board accepts a review hold as plan.item.update with
# changes.review.hold alone, and its own notice tells reviewers to send it,
# but the contract listed only look_at and could_not_verify, so discovery
# could not show the carrier (Infra's W537 acceptance, 2026-10-05).
def test_the_update_contract_describes_the_review_hold_and_its_alone_rule():
    contract = cli._coordinate_command(_args("plan.item.update", contract=True))["contract"]
    hold = contract["payload"]["changes"]["review"]["hold"]
    assert set(hold) == {"waiting_on", "reason", "due_at"}
    assert "operator" in hold["waiting_on"] and "clears" in hold["waiting_on"]
    assert "14 days" in hold["due_at"]
    assert "changes.review.hold sent alone" in contract["description"]
    assert "review.return" in contract["description"]


def test_a_hold_only_update_passes_the_local_shape_check_and_is_sent_unchanged(submits, monkeypatch, tmp_path):
    host, identity, _channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = _update(
        {"review": {"hold": {"waiting_on": "codex-api", "reason": "Merging the reviewed head.",
                             "due_at": "2026-10-06T12:00:00Z"}}},
        key="w537-hold",
    )

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "No result before the deadline.", status=504)

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    with pytest.raises(DomainError) as unknown:
        cli._coordinate_command(
            _args("plan.item.update", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
        )
    assert unknown.value.code == "work_coordinate_outcome_unknown"
    assert [submitted["payload"] for submitted in submits] == [payload]


def test_a_dotted_or_non_object_hold_is_refused_locally_and_nothing_is_sent(submits, tmp_path):
    with pytest.raises(DomainError) as dotted:
        cli._coordinate_command(_args(
            "plan.item.update", object_ref=PROJECT,
            payload=_update({"review": {"hold.waiting_on": "codex-api", "hold.due_at": "2026-10-06T12:00:00Z"}}),
            config=str(tmp_path / "no-relay.json"),
        ))
    [problem] = dotted.value.details["problems"]
    assert problem["problem"] == "dotted"
    assert json.loads(problem["corrected"]) == {
        "hold": {"waiting_on": "codex-api", "due_at": "2026-10-06T12:00:00Z"},
    }
    with pytest.raises(DomainError) as wrong_type:
        cli._coordinate_command(_args(
            "plan.item.update", object_ref=PROJECT,
            payload=_update({"review": {"hold": "codex-api until tomorrow"}}),
            config=str(tmp_path / "no-relay.json"),
        ))
    assert [(p["field"], p["problem"]) for p in wrong_type.value.details["problems"]] == [
        ("changes.review.hold", "type")
    ]
    assert submits == []


def test_the_nested_update_is_sent_unchanged_under_its_own_request_identity(submits, monkeypatch, tmp_path):
    host, identity, _channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = _update(
        {
            "result": "Merged and active.",
            "review": {"look_at": "Run the suite.", "could_not_verify": "None"},
            # A field this client does not list is the service's to judge.
            "a_field_a_newer_service_accepts": "kept",
        }
    )

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "No result before the deadline.", status=504)

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    with pytest.raises(DomainError) as unknown:
        cli._coordinate_command(
            _args("plan.item.update", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
        )
    assert unknown.value.code == "work_coordinate_outcome_unknown"
    assert len(submits) == 1
    assert submits[0]["payload"] == payload
    assert unknown.value.details["recovery"]["request_hash"] == coordinate_request_hash("plan.item.update", PROJECT, payload)


def test_work_status_set_advertises_every_canonical_status_including_working(submits, tmp_path):
    from project_board.contract.operation_shapes import operation_call_problems
    from project_board.contract.work_lifecycle import CANONICAL_WORK_STATUSES

    contract = cli._coordinate_command(_args("work.status.set", contract=True))["contract"]
    assert contract["payload"]["status"].split(" | ") == list(CANONICAL_WORK_STATUSES)
    assert "working" in contract["payload"]["status"]
    example = json.loads(contract["example"].split("--payload-json ", 1)[1].strip("'"))
    assert example["status"] == "working" and isinstance(example["expected_revision"], int)
    base = {"work_ref": WORK_REF, "expected_revision": 5, "idempotency_key": "status-1"}
    for status in (*CANONICAL_WORK_STATUSES, "Working"):
        assert operation_call_problems("work.status.set", PROJECT, {**base, "status": status}) == [], status

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args("work.status.set", object_ref=PROJECT, payload={**base, "status": "in_progress"},
                  config=str(tmp_path / "no-relay.json"))
        )
    assert [(p["field"], p["problem"]) for p in refused.value.details["problems"]] == [("status", "invalid")]
    assert submits == []


def test_a_dotted_review_on_work_status_set_is_refused_with_the_nested_form(submits, tmp_path):
    payload = {"work_ref": WORK_REF, "expected_revision": 5, "idempotency_key": "status-2",
               "status": "review", "review.look_at": "Run the suite.", "review.could_not_verify": "None"}
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args("work.status.set", object_ref=PROJECT, payload=payload, config=str(tmp_path / "no-relay.json"))
        )
    [problem] = refused.value.details["problems"]
    assert problem["problem"] == "dotted"
    assert json.loads(problem["corrected"])["review"] == {"look_at": "Run the suite.", "could_not_verify": "None"}
    assert submits == []


def test_every_example_keeps_each_nested_catalog_field_an_object():
    """A nested field never appears as a string placeholder (W404, review.assign integration)."""

    def typed(value, shape, path, wrong):
        if isinstance(shape, dict):
            if not isinstance(value, dict):
                wrong.append(path)
                return
            for name, child in value.items():
                typed(child, shape.get(name), f"{path}.{name}", wrong)

    wrong: list[str] = []
    for operation, shape in PROBLEM_BOARD_OPERATION_SHAPES.items():
        example = json.loads(operation_contract(operation)["example"].split("--payload-json ", 1)[1].strip("'"))
        payload = shape.get("payload") if isinstance(shape.get("payload"), dict) else {}
        for field, value in example.items():
            typed(value, payload.get(field), f"{operation}:{field}", wrong)
    assert wrong == []


def test_review_assign_says_the_reviewer_becomes_the_assignee():
    # 2026-09-30 02:37Z: routing W403 to App made App its assignee, reviewer
    # and acting holder, while the contract said the assignee stays the last worker.
    from project_board.contract.worker_operation_contract import PROBLEM_BOARD_OPERATION_POLICIES

    contract = operation_contract("review.assign")
    for text in (contract["description"], PROBLEM_BOARD_OPERATION_POLICIES["review.assign"]["description"]):
        assert "stays the last worker" not in text
        assert "reviewer becomes the item's assignee" in text
    example = json.loads(contract["example"].split("--payload-json ", 1)[1].strip("'"))
    assert example["integration"] == {"merged": ["<merge commit>"], "deploy": "<window>: <check>"}


# W404 review of d78fd7a4: a null review was sent, and building the
# correction renamed an unknown field and changed the caller's payload.


def test_a_null_review_is_refused_locally_and_nothing_is_sent(submits, tmp_path):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(
            _args("plan.item.update", object_ref=PROJECT, payload=_update({"review": None}),
                  config=str(tmp_path / "no-relay.json"))
        )
    assert refused.value.code == "work_coordinate_shape_invalid"
    assert [(p["field"], p["problem"]) for p in refused.value.details["problems"]] == [("changes.review", "type")]
    assert "Null is not accepted." in refused.value.details["problems"][0]["message"]
    assert submits == []


def test_null_stays_accepted_where_the_service_reads_it_as_a_default():
    from project_board.contract.operation_shapes import operation_call_problems

    assert operation_call_problems("plan.item.update", PROJECT, _update({"review_requirement": None})) == []
    status = {"work_ref": WORK_REF, "expected_revision": 5, "idempotency_key": "s", "status": "working", "review": None}
    assert operation_call_problems("work.status.set", PROJECT, status) == []


def test_the_correction_keeps_unknown_fields_and_leaves_the_call_unchanged():
    import copy

    from project_board.contract.operation_shapes import operation_call_problems

    changes = {
        "review": {"could_not_verify": "None"},
        "review.look_at": "Run the suite.",
        "a_new_service_field.value": "untouched",
    }
    payload = _update(changes)
    before = copy.deepcopy(payload)
    [problem] = operation_call_problems("plan.item.update", PROJECT, payload)
    assert payload == before, "building the correction changes nothing the caller passed"
    assert json.loads(problem["corrected"]) == {
        "review": {"could_not_verify": "None", "look_at": "Run the suite."},
        "a_new_service_field.value": "untouched",
    }


@pytest.mark.parametrize("dotted_first", [True, False])
def test_the_correction_keeps_nested_siblings_in_either_key_order(dotted_first):
    # W404 review of dbd74c1f: with the dotted name first, the nested
    # object's sibling was dropped by a shallow merge.
    from project_board.contract.operation_shapes import operation_call_problems

    dotted = ("review.future_service_field.add", "new")
    nested = ("review", {"future_service_field": {"keep": "original"}})
    changes = dict([dotted, nested] if dotted_first else [nested, dotted])
    [problem] = operation_call_problems("plan.item.update", PROJECT, _update(changes))
    assert json.loads(problem["corrected"]) == {
        "review": {"future_service_field": {"keep": "original", "add": "new"}}
    }


# W551, operator 2026-10-05: "all work items must have W"; "when W created it
# gets the next free number". The board numbers each new item; the client
# contract says so, and a caller-chosen key is refused before anything is sent.
def test_the_create_contract_says_the_board_numbers_the_item():
    contract = cli._coordinate_command(_args("plan.item.create", contract=True))["contract"]
    assert "next free W number" in contract["description"]
    assert "work_item_key_allocated_by_board" in contract["description"]
    assert "without item_key" in contract["payload"]["item"]


def test_a_supplied_item_key_is_refused_locally_and_nothing_is_sent(submits, tmp_path):
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_args(
            "plan.item.create", object_ref=PROJECT,
            payload={"item": {"item_key": "maintenance-readiness-20261005", "title": "T"}, "idempotency_key": "k"},
            config=str(tmp_path / "no-relay.json"),
        ))
    assert refused.value.code == "work_coordinate_shape_invalid"
    assert [(p["field"], p["problem"]) for p in refused.value.details["problems"]] == [
        ("item.item_key", "allocated_by_board")
    ]
    assert submits == []


def test_a_numberless_create_is_sent_unchanged(submits, monkeypatch, tmp_path):
    host, identity, _channel = make_host(tmp_path)
    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *args, **kwargs: None)
    payload = {"item": {"title": "Filed without a key", "description": "d"}, "idempotency_key": "w551-create"}

    def outcome_unknown(queue_, path, *, worker_name, request_id, timeout_seconds):
        raise DomainError("work_coordinate_outcome_unknown", "No result before the deadline.", status=504)

    monkeypatch.setattr(cli, "_await_coordinate_response", outcome_unknown)
    with pytest.raises(DomainError) as unknown:
        cli._coordinate_command(
            _args("plan.item.create", object_ref=PROJECT, payload=payload, config=str(host.path), identity=identity)
        )
    assert unknown.value.code == "work_coordinate_outcome_unknown"
    assert [submitted["payload"] for submitted in submits] == [payload]

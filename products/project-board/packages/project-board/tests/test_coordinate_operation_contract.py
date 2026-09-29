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

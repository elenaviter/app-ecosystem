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
from project_board.client.coordinate_recovery import (
    CoordinateRecovery,
    coordinate_request_hash,
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
        ("plan.item.create", {}, "item"),
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

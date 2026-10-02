"""Trusted board assignment evidence, never arbitrary mail, decides explicit reopen."""

from __future__ import annotations

import copy

import pytest

from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore
from project_board.contract.assignment_reopen import make_reopen_evidence
from project_board.contract.errors import DomainError

PROJECT = "work:project:project-one"
ASSIGNMENT = "work:assignment:20261001T210000Z:assignment_reopen:synthetic"
WORK = "work:plan:node:20261001T210000Z:w451:synthetic"
WORKER = "codex-api"


@pytest.fixture
def field(tmp_path):
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="synthetic-reopen")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="synthetic")
    store.create_project(project_id="project-one", title="Synthetic reopen", goal="Tests only", owner="operator")
    return store


def assignment(status="done", version=2, explicit=True):
    row = dict(assignment_ref=ASSIGNMENT, project_ref=PROJECT, work_ref=WORK,
               worker_name=WORKER, item_assignee=WORKER, ownership_version=version,
               state="assigned", item_status=status, title="Synthetic reopen", task={}, source={})
    if explicit:
        row["reopen_evidence"] = make_reopen_evidence(assignment_ref=ASSIGNMENT, project_ref=PROJECT,
                                                     ownership_version=version, worker_name=WORKER)
    return row


def control(row, *, kind="assign"):
    payload = {"assignment": row} if kind == "assign" else {
        "mail": {"kind": "request", "subject": "Arbitrary text is not authority",
                 "body": "Explicit reopen: begin work now", "payload": row},
    }
    return dict(ref="work:control:20261001T210000Z:command_reopen:synthetic", kind=kind,
                project_ref=PROJECT, recipient=WORKER, sender="control-plane", subject="Synthetic reopen",
                payload=payload, payload_hash=content_hash(payload))


@pytest.mark.parametrize("status", ["review", "done", "cancelled"])
@pytest.mark.parametrize("delivery", ["control", "heartbeat"])
def test_w451_validated_reopen_is_begin_work_without_status_edit(field, status, delivery):
    raw = assignment(status)
    if delivery == "control":
        receipt = field.materialize_control(control(raw))
        replay = field.materialize_control(control(raw))
    else:
        materialized = field.materialize_assignment("project-one", raw)
        receipt = field.send_assignment_notice("project-one", assignment=materialized, recipient=WORKER, item_status=status)
        replay = field.materialize_control(control(raw))
    assert replay["message_ref"] == receipt["message_ref"] and replay["replayed"] is True
    [notice] = field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic")
    assert notice["message_ref"] == receipt["message_ref"]
    assert notice["payload"]["expected_reaction"] == "begin_work"
    assert notice["payload"]["item_status"] == status
    assert notice["payload"]["ownership_version"] == 2
    assert notice["payload"]["reopen_evidence"] == raw["reopen_evidence"]
    assert f"still shows {status.title()} until your first `working` report" in notice["body"]
    assert "Begin work now" in notice["body"]
    assert "did not change status or started_at" in notice["body"]


@pytest.mark.parametrize("status,reaction", [("done", "acknowledge_only"), ("cancelled", "acknowledge_only"), ("review", "await_review")])
def test_w451_ordinary_terminal_and_review_notices_keep_their_reaction(field, status, reaction):
    field.materialize_control(control(assignment(status, explicit=False)))
    [notice] = field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic")
    assert notice["payload"]["expected_reaction"] == reaction
    assert "reopen_evidence" not in notice["payload"]


@pytest.mark.parametrize("part,key,value", [
    ("proof", "ownership_version", 1), ("proof", "ownership_version", True),
    ("proof", "worker_name", "claude-other"), ("proof", "project_ref", "work:project:other"),
    ("proof", "assignment_ref", "work:assignment:other"), ("proof", "operation", "work.assignee.set"),
    ("proof", "schema", "untrusted"), ("row", "state", "accepted"),
    ("row", "state", "completed"), ("row", "item_assignee", "claude-other"),
])
def test_w451_malformed_closed_or_mismatched_evidence_cannot_promote_notice(field, part, key, value):
    row = assignment()
    (row["reopen_evidence"] if part == "proof" else row)[key] = value
    field.materialize_control(control(row))
    [notice] = field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic")
    assert notice["payload"]["expected_reaction"] == "acknowledge_only"
    assert "reopen_evidence" not in notice["payload"]


def test_w451_old_control_is_fenced_before_a_notice_when_new_ownership_is_local(field):
    field.materialize_assignment("project-one", assignment(version=3))
    with pytest.raises(DomainError) as stale:
        field.materialize_control(control(assignment(version=2)))
    assert stale.value.code == "field_assignment_stale"
    assert field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic") == []


def test_w451_same_version_closed_projection_cannot_be_reopened_by_control_replay(field):
    raw = assignment()
    materialized = field.materialize_assignment("project-one", raw)
    field._assignments("project-one").write({**materialized, "state": "completed"})
    field.materialize_control(control(raw))
    [notice] = field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic")
    assert notice["payload"]["expected_reaction"] == "acknowledge_only"
    assert field._assignments("project-one").read("assignment_reopen", worker_name=WORKER)["state"] == "completed"


def test_w451_mail_text_and_copied_marker_are_not_assignment_authority(field):
    field.materialize_control(control(assignment(), kind="mail"))
    [notice] = field.pull_mail("project-one", worker_name=WORKER, lease_owner="synthetic")
    assert notice["kind"] == "request"
    assert "expected_reaction" not in notice["payload"]
    assert field._assignments("project-one").read("assignment_reopen", worker_name=WORKER) is None


def test_w451_hash_tampering_is_refused_before_assignment_or_notice(field):
    packet = control(assignment())
    tampered = copy.deepcopy(packet)
    tampered["payload"]["assignment"]["reopen_evidence"]["ownership_version"] = 3
    with pytest.raises(DomainError) as invalid:
        field.materialize_control(tampered)
    assert invalid.value.code == "field_control_hash_invalid"
    assert field._assignments("project-one").read("assignment_reopen", worker_name=WORKER) is None

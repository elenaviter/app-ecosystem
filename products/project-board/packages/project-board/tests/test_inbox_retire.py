"""pb worker inbox-retire settles only superseded board notices, audited (W563).

Why: a coordinator back from a pause found hundreds of pending messages, and
settling each meant receiving it, body and all. Coordinator decision
2026-10-05 21:19Z: retirement is never by age or kind alone, because an
``update`` written by a person or an agent can carry a GO or a scope decision.
Only a typed board notice with evidence that it is superseded is retired; the
dry run binds its selection to a digest, and apply settles nothing when the
selection changed after review.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.render import render_envelope
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError


PROJECT = "retire-project"
PROJECT_REF = f"work:project:{PROJECT}"
WORKER = "claude-main"
PEER = "codex-app"
OTHER = "claude-other"
ITEM_A = "work:plan:node:20261005T100000Z:w1:item-a"
ITEM_B = "work:plan:node:20261005T100000Z:w2:item-b"
ITEM_C = "work:plan:node:20261005T100000Z:w3:item-c"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="retire-test")
    for name in (WORKER, PEER, OTHER):
        store.register_worker(worker_name=name, runtime_kind="codex", capabilities=[], authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT, title="Retire", goal="Retire superseded notices only.", owner="operator")
    for name in (WORKER, OTHER):
        store.sync_worker_attendances(name, [PROJECT_REF])
        store.listen_worker(name)
    return store


@pytest.fixture
def items(monkeypatch) -> dict:
    """The board's current item state, as project.plan.item would return it."""

    state = {
        ITEM_A: {"status": "done", "assignee": "codex-app", "reviewer": "", "revision": 9},
        ITEM_B: {"status": "review", "assignee": "codex-app", "reviewer": "claude-main", "revision": 4},
        ITEM_C: {"status": "working", "assignee": "codex-app", "reviewer": "", "revision": 7},
    }

    def request(args, *, action, object_ref, payload):
        assert action == "project.plan.item" and object_ref == PROJECT_REF
        return {"object": dict(state[payload["work_ref"]])}

    monkeypatch.setattr(cli, "_reference_mapping_request", request)
    return state


def _notice(field, number, payload, *, kind="update", recipient=WORKER, subject="Board notice"):
    return field.send_mail(
        PROJECT, sender="control-plane", recipient=recipient, kind=kind, subject=subject,
        body=f"NOTICE_BODY_{number}", payload=payload, idempotency_key=f"notice-{number}",
    )


def _review_sits(field, number, item):
    return _notice(field, number, {"identity_ref": item, "reason": "review_sits", "entered_at": "2026-10-05T10:00:00Z", "hold": None})


def _responsibility(field, number, item, assignee):
    return _notice(field, number, {
        "work_ref": item, "identity_ref": item, "assignee": assignee, "previous_assignee": "", "item_status": "working",
    })


def _announcement(field, number, revision):
    return _notice(field, number, {"coordinator_announcement": {
        "holder_worker_name": "codex-main", "home_worker_name": "codex-main", "acting": False,
        "home_available": True, "revision": revision,
    }})


def _retire(field, **options):
    args = types.SimpleNamespace(apply=False, digest="", approval_ref="", runtime_kind="", runtime_session_id="", config=None)
    for key, value in options.items():
        setattr(args, key, value)
    return cli._worker_inbox_retire(field, types.SimpleNamespace(worker_name=WORKER, runtime_session_id=WORKER), args)


def _pending_refs(field) -> set[str]:
    return {header["message_ref"] for header in field.pending_mail_headers(WORKER)}


@pytest.fixture
def backlog(field, items) -> dict:
    refs = {
        "done_review": _review_sits(field, 1, ITEM_A)["message_ref"],
        "open_review": _review_sits(field, 2, ITEM_B)["message_ref"],
        "older_responsibility": _responsibility(field, 3, ITEM_C, "claude-old")["message_ref"],
        "newer_responsibility": _responsibility(field, 4, ITEM_C, "codex-app")["message_ref"],
        "older_announcement": _announcement(field, 5, 3)["message_ref"],
        "newer_announcement": _announcement(field, 6, 5)["message_ref"],
        "untyped": _notice(field, 7, {"something": "else"})["message_ref"],
        "with_control_ref": _notice(field, 8, {
            "identity_ref": ITEM_A, "reason": "x", "entered_at": "", "hold": None, "command_ref": "work:control:c-1",
        })["message_ref"],
        "disk_decision": _notice(field, 9, {"notice_kind": "host.disk.low", "disk_usage": {}}, kind="decision")["message_ref"],
        # Labelled information, written by an agent, carrying an action: the
        # 2026-10-05 case where an update held a runtime GO.
        "person_update": field.send_mail(
            PROJECT, sender=PEER, recipient=WORKER, kind="update", subject="FYI status",
            body="GO granted for the window; also the scope changed.", idempotency_key="person-update",
        )["message_ref"],
    }
    # A notice in another worker's mailbox is never read or retired.
    refs["other_worker"] = _review_sits_for(field, 10, ITEM_A, OTHER)["message_ref"]
    return refs


def _review_sits_for(field, number, item, recipient):
    return _notice(field, number, {"identity_ref": item, "reason": "review_sits", "entered_at": "", "hold": None}, recipient=recipient)


def test_the_dry_run_selects_only_superseded_typed_board_notices_and_changes_nothing(field, backlog):
    pending_before = _pending_refs(field)

    result = _retire(field)
    plan = json.loads(Path(result["selection_file"]).read_text(encoding="utf-8"))
    selected = {entry["message_ref"]: entry for entry in plan["selected"]}
    reasons = {entry["message_ref"]: entry["reason"] for entry in plan["excluded"]}

    assert set(selected) == {backlog["done_review"], backlog["older_responsibility"], backlog["older_announcement"]}
    assert selected[backlog["done_review"]]["evidence"] == {"item_revision": 9, "status": "done"}
    assert selected[backlog["older_responsibility"]]["evidence"] == {"superseded_by": backlog["newer_responsibility"]}
    assert selected[backlog["older_announcement"]]["evidence"] == {"superseded_by": backlog["newer_announcement"]}
    assert reasons == {
        backlog["open_review"]: "still_current",
        backlog["newer_responsibility"]: "still_current",
        backlog["newer_announcement"]: "still_current",
        backlog["untyped"]: "untyped_notice",
        backlog["with_control_ref"]: "untyped_notice",
        backlog["disk_decision"]: "kind_decision",
        backlog["person_update"]: "not_a_board_notice",
    }
    assert backlog["other_worker"] not in selected and backlog["other_worker"] not in reasons
    assert result["mode"] == "dry_run" and result["selected"] == 3 and result["digest"] == plan["digest"]
    # Nothing leased or settled, and no body in what the model reads.
    assert _pending_refs(field) == pending_before
    text = render_envelope({"ok": True, "result": result})
    assert "NOTICE_BODY" not in text and "GO granted" not in text
    assert "NOTICE_BODY" not in Path(result["selection_file"]).read_text(encoding="utf-8")


def test_apply_settles_exactly_the_reviewed_selection_once(field, backlog):
    reviewed = _retire(field)

    applied = _retire(field, apply=True, digest=reviewed["digest"], approval_ref="work:mail:approval-1")

    assert applied["state"] == "applied" and applied["receipt"]["settled"] == 3
    assert applied["receipt"]["approval_ref"] == "work:mail:approval-1"
    retired = {backlog["done_review"], backlog["older_responsibility"], backlog["older_announcement"]}
    assert not (retired & _pending_refs(field))
    assert {backlog["person_update"], backlog["open_review"], backlog["newer_announcement"]} <= _pending_refs(field)
    assert field.list_worker_mail_leases(WORKER, lease_owner=WORKER, cursor="", limit=20)["total"] == 0

    again = _retire(field, apply=True, digest=reviewed["digest"], approval_ref="work:mail:approval-1")
    assert again["state"] == "already_applied" and again["receipt"]["settled"] == 3


def test_apply_settles_nothing_when_the_item_changed_after_the_dry_run(field, backlog, items):
    reviewed = _retire(field)
    pending_before = _pending_refs(field)
    items[ITEM_A]["status"] = "review"  # reopened for review after the dry run

    with pytest.raises(DomainError) as refused:
        _retire(field, apply=True, digest=reviewed["digest"], approval_ref="work:mail:approval-1")

    assert refused.value.code == "field_inbox_retire_selection_changed"
    assert _pending_refs(field) == pending_before


def test_apply_settles_nothing_when_a_selected_message_was_leased_after_the_dry_run(field, backlog):
    reviewed = _retire(field)
    pull_worker_input(field, worker_name=WORKER, message_ref=backlog["done_review"])

    with pytest.raises(DomainError) as refused:
        _retire(field, apply=True, digest=reviewed["digest"], approval_ref="work:mail:approval-1")

    assert refused.value.code == "field_inbox_retire_selection_changed"
    assert backlog["older_responsibility"] in _pending_refs(field)


def test_the_store_refuses_a_changed_or_missing_message_before_leasing_anything(field, backlog):
    plan = json.loads(Path(_retire(field)["selection_file"]).read_text(encoding="utf-8"))
    approved = {entry["message_ref"]: entry["content_hash"] for entry in plan["selected"]}
    pending_before = _pending_refs(field)

    with pytest.raises(DomainError, match="changed after the dry run"):
        field.retire_mail(WORKER, lease_owner=WORKER, approved={**approved, backlog["done_review"]: "0" * 64}, summaries={})
    with pytest.raises(DomainError, match="no longer pending"):
        field.retire_mail(WORKER, lease_owner=WORKER, approved={**approved, "work:mail:20261005T000000Z:mail_gone:gone": "x"}, summaries={})

    assert _pending_refs(field) == pending_before
    assert field.list_worker_mail_leases(WORKER, lease_owner=WORKER, cursor="", limit=20)["total"] == 0


def test_apply_needs_the_reviewed_digest_and_an_approval_ref(field, backlog):
    reviewed = _retire(field)
    with pytest.raises(DomainError) as missing:
        _retire(field, apply=True, digest=reviewed["digest"])
    assert missing.value.code == "field_inbox_retire_approval_required"
    with pytest.raises(DomainError) as stale:
        _retire(field, apply=True, digest="0" * 64, approval_ref="work:mail:approval-1")
    assert stale.value.code == "field_inbox_retire_selection_changed"


def test_an_unreadable_item_keeps_its_notices_pending(field, monkeypatch):
    _review_sits(field, 1, ITEM_A)

    def unreachable(args, *, action, object_ref, payload):
        raise DomainError("work_send_channel_reconnecting", "The board is unreachable.")

    monkeypatch.setattr(cli, "_reference_mapping_request", unreachable)
    plan = json.loads(Path(_retire(field)["selection_file"]).read_text(encoding="utf-8"))
    assert plan["selected"] == []
    assert [entry["reason"] for entry in plan["excluded"]] == ["evidence_unavailable"]


@pytest.mark.parametrize("item", [
    {},
    {"status": None, "assignee": "x", "reviewer": "", "revision": 3},
    {"status": "in_review", "assignee": "x", "reviewer": "", "revision": 3},
    {"status": "done", "assignee": "x", "reviewer": "", "revision": 0},
    {"status": "done", "assignee": "x", "reviewer": "", "revision": "9"},
])
def test_an_incomplete_item_is_no_evidence_for_a_review_notice(field, monkeypatch, item):
    # Review of bef2c40c: a missing or unknown status must not read as "the
    # item left Review", or a response-shape drift retires every such notice.
    _review_sits(field, 1, ITEM_A)
    monkeypatch.setattr(cli, "_reference_mapping_request", lambda args, **_: {"object": dict(item)})

    plan = json.loads(Path(_retire(field)["selection_file"]).read_text(encoding="utf-8"))
    assert plan["selected"] == []
    assert [entry["reason"] for entry in plan["excluded"]] == ["evidence_unavailable"]


def test_a_missing_assignee_is_no_evidence_for_a_responsibility_notice(field, monkeypatch):
    _responsibility(field, 1, ITEM_C, "codex-app")
    monkeypatch.setattr(
        cli, "_reference_mapping_request",
        lambda args, **_: {"object": {"status": "working", "reviewer": "", "revision": 7}},
    )

    plan = json.loads(Path(_retire(field)["selection_file"]).read_text(encoding="utf-8"))
    assert plan["selected"] == []
    assert [entry["reason"] for entry in plan["excluded"]] == ["evidence_unavailable"]


def test_each_settlement_names_its_retirement_and_approval(field, backlog):
    reviewed = _retire(field)
    _retire(field, apply=True, digest=reviewed["digest"], approval_ref="work:mail:approval-1")

    for ref in (backlog["done_review"], backlog["older_responsibility"], backlog["older_announcement"]):
        record = json.dumps(field._mail_record_unlocked(PROJECT, WORKER, ref))
        assert "approved by work:mail:approval-1" in record and reviewed["retirement_id"] in record

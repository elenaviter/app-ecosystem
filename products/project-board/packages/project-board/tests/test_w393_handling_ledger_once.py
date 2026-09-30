"""A task body prints once in the brief handling ledger (W393).

2026-09-30 03:05Z: a review notice carried its whole task as the body and
again as ``payload.command.instructions``. ``pb worker receive`` and
``pb worker lease-read`` printed both, so every review cost the task twice.
The fixtures below have that exact structure, with synthetic content, and are
rendered through the real ``pb render`` command as well as the renderer.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.render import render_envelope

PROJECT = "work:project:compact-ledger-" + "p" * 20
WORK_REF = "work:plan:node:20260929T181058Z:w94:" + "operation-contract-executable-" * 3
TAIL = "TASK_TAIL_SENTINEL"


def _task(index: int, *, paragraphs: int = 12) -> str:
    lines = [f"# Review W94 part {index}", ""]
    for number in range(paragraphs):
        lines.append(f"Paragraph {number}: check the exact head, the gates and the owning rule. " * 6)
        lines.append("")
    lines.append(f"{TAIL} {index}")
    return "\n".join(lines)


def _message(index: int, *, instructions: str | None = None, body: str | None = None) -> dict:
    task = body if body is not None else _task(index)
    command_ref = f"work:control:20260930T025555Z:command_{index:032d}:review-w94"
    return {
        "kind": "request",
        "sender": "control-plane",
        "recipient": "codex-00000000-0000-4000-8000-000000000001",
        "subject": f"Review W94 part {index}",
        "created_at": "2026-09-30T02:56:14.125642Z",
        "state": "leased",
        "delivery_status": "pending",
        "message_id": f"mail_{index:032d}",
        "message_ref": f"work:mail:20260930T025614Z:mail_{index:032d}:review-w94-part-{index}",
        "correlation_id": command_ref,
        "idempotency_key": f"control:{command_ref}",
        "work_ref": WORK_REF,
        "project_ref": PROJECT,
        "attachment_count": 0,
        "attachments": [],
        "body": task,
        "lease": {"lease_id": f"lease_{index:032d}", "expires_at": "2026-09-30T03:33:57Z"},
        "payload": {
            "command": {
                "instructions": task if instructions is None else instructions,
                "review_request": {
                    "item_ref": WORK_REF,
                    "reviewer": "codex-00000000-0000-4000-8000-000000000001",
                    "worked_by": "claude-code-00000000-0000-4000-8000-000000000002",
                },
            },
            "command_ref": command_ref,
            "identity_ref": WORK_REF,
            "payload_hash": "h" * 64,
            "work_ref": WORK_REF,
        },
    }


def _lease_read(message: dict) -> dict:
    return {
        "ok": True,
        "result": {
            "schema": "problem-board.lease-read.v1",
            "project_ref": PROJECT,
            "message": message,
            "settlement": {"required": True, "outcomes": ["acknowledged", "refused"]},
            "replayed": True,
        },
    }


def _receive(messages: list[dict], *, held: int) -> dict:
    return {
        "ok": True,
        "result": {
            "schema": "problem-board.worker-input.v2",
            "delivery": {"item_count": len(messages), "remaining_count": 2, "has_more": True, "limited_by": "size"},
            "acquired_leases": [
                {"message_ref": m["message_ref"], "lease_id": m["lease"]["lease_id"], "expires_at": m["lease"]["expires_at"]}
                for m in messages
            ],
            "active_leases": {"acquired_now_count": len(messages), "already_held_count": held - len(messages), "total_held_count": held},
            "projects": [{"project_ref": PROJECT, "revision": 362, "leased_messages": len(messages)}],
            "items": [{"project_ref": PROJECT, "message": m} for m in messages],
        },
    }


def _marker(task: str) -> str:
    text = task.strip()
    return (
        f"  command.instructions = (identical to the body above: {len(text.encode('utf-8'))} bytes, "
        f"sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]})"
    )


def _keeps_the_handling_ledger(text: str, message: dict) -> None:
    lines = text.splitlines()
    for key in ("message_ref", "correlation_id", "work_ref", "idempotency_key"):
        assert f"{key}: {message[key]}" in lines, key
    assert f"lease_id: {message['lease']['lease_id']}" in lines
    assert f"  command_ref = {message['payload']['command_ref']}" in lines
    assert f"  identity_ref = {message['payload']['identity_ref']}" in lines
    assert f"  command.review_request.worked_by = {message['payload']['command']['review_request']['worked_by']}" in lines
    assert any(line.startswith("read: pb worker lease-read ") and message["message_ref"] in line for line in lines)
    assert any(line.startswith("settle: pb worker settle ") and message["lease"]["lease_id"] in line for line in lines)
    assert any(line.startswith("reply: pb worker send ") and message["correlation_id"] in line for line in lines)


def test_a_lease_read_prints_the_task_once_and_names_its_copy():
    message = _message(1)
    text = render_envelope(_lease_read(message))
    assert text.count(f"{TAIL} 1") == 1, "the task is printed once"
    assert _marker(message["body"]) in text.splitlines()
    _keeps_the_handling_ledger(text, message)
    assert "settlement: required True · outcomes acknowledged, refused" in text


def test_instructions_that_differ_from_the_body_stay_whole():
    message = _message(2, instructions=_task(2) + "\n\nOne more step: rerun the merged tree.")
    text = render_envelope(_lease_read(message))
    assert text.count(f"{TAIL} 2") == 2, "a near copy is not the body: both stay"
    assert "One more step: rerun the merged tree." in text
    assert "identical to the body above" not in text


def test_a_receive_with_several_leases_prints_each_task_once_within_budget():
    messages = [_message(index) for index in range(1, 4)]
    messages.append(_message(4, instructions="A different, shorter instruction."))
    text = render_envelope(_receive(messages, held=6))
    for message in messages:
        index = int(message["subject"].rsplit(" ", 1)[1])
        assert text.count(f"{TAIL} {index}") == 1, index
        _keeps_the_handling_ledger(text, message)
    assert text.count("identical to the body above") == 3
    assert "  command.instructions = A different, shorter instruction." in text.splitlines()
    assert "delivery: items 4 · remaining 2 · has_more True · limited_by size" in text
    assert "leases: acquired now 4 · already held 2 · total held 6" in text
    assert "NOTE: 2 held lease(s) are not in this batch." in text
    # Each task costs its own size once, plus a bounded ledger per item.
    task_bytes = sum(len(m["body"].encode("utf-8")) for m in messages)
    assert len(text.encode("utf-8")) <= task_bytes * 1.1 + 2_500 * len(messages), len(text.encode("utf-8"))


def test_a_large_task_is_never_cut():
    body = _task(5, paragraphs=400)
    message = _message(5, body=body)
    text = render_envelope(_lease_read(message))
    assert len(body.encode("utf-8")) > 150_000
    for line in body.splitlines():
        assert line in text or not line
    assert text.count(f"{TAIL} 5") == 1


def test_the_brief_view_never_changes_the_envelope_json_prints():
    envelope = _lease_read(_message(6))
    before = copy.deepcopy(envelope)
    render_envelope(envelope)
    assert envelope == before, "--format json prints the envelope with every copy"


@pytest.mark.parametrize("builder", ["lease_read", "receive"])
def test_pb_render_prints_the_same_ledger(tmp_path: Path, capsys, builder):
    message = _message(7)
    envelope = _lease_read(message) if builder == "lease_read" else _receive([message], held=1)
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(envelope), encoding="utf-8")

    exit_code = cli.main(["render", "--file", str(saved)])

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert printed == render_envelope(envelope)
    assert printed.count(f"{TAIL} 7") == 1
    _keeps_the_handling_ledger(printed, message)

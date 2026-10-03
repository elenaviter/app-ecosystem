"""W472: the receive brief view leaves out what the recipient never acts on.

A receive item in brief output carried the sender's idempotency key, empty
envelope defaults and every attachment's read command one argv element per
line. The model reads all of it. The compact receive view keeps the body, every
ref, the correlation, the lease and the follow-up commands, keeps every nested
task or review field and every distinct provenance ref, and prints each
attachment as one summary with its exact file_ref and a runnable read command.
``pb worker lease-read`` and ``--format json`` keep the complete message.
"""

from __future__ import annotations

import copy
import json
import shlex

import pytest

from project_board.client.render import render_envelope


FLAGS = ["--runtime-kind", "claude-code", "--runtime-session-id", "session-one"]
PROJECT = "work:project:quickstart"
MESSAGE_REF = "work:mail:20261003T110404769283Z:mail_582815e2:design-feedback"
SOURCE_REF = "work:mail:20261003T110401694995Z:mail_711954bf:design-feedback"
COMMAND_REF = "work:control:20261003T110402232369Z:command_7f3b83d2:design-feedback"
WORK_REF = "work:plan:node:20261002T215924Z:w472:data-protocol"
LEASE = "lease_994371abfa59434a90fc04fda9183bcb"
IDEMPOTENCY = "control:" + COMMAND_REF


def _message(**overrides):
    message = {
        "message_ref": MESSAGE_REF,
        "kind": "reply",
        "sender": "codex-reviewer",
        "recipient": "claude-author",
        "subject": "W472 design feedback",
        "created_at": "2026-10-03T11:04:04.769283Z",
        "state": "leased",
        "delivery_status": "pending",
        "correlation_id": "w472-start",
        "reply_to": "work:mail:20261003T110215Z:mail_33ab:checkpoint",
        "idempotency_key": IDEMPOTENCY,
        "project_ref": PROJECT,
        "body": "Keep provenance.\nQuote attachment commands.",
        "payload": {
            "command_ref": COMMAND_REF,
            "identity_ref": "",
            "operator_origin": {"channel": "unknown", "ref": ""},
            "payload": {},
            "payload_hash": "125058468a4d6c2355caa0f5a5743a1a5436c2f6db2a838f1dba3f8a41232f8c",
            "source_message_ref": SOURCE_REF,
            "work_ref": "",
        },
    }
    message.update(overrides)
    return message


def _receive(*messages):
    return {
        "ok": True,
        "result": {
            "schema": "problem-board.worker-input.v1",
            "delivery": {"item_count": len(messages), "remaining_count": 0, "has_more": False},
            "acquired_leases": [
                {"message_ref": m["message_ref"], "lease_id": f"{LEASE}{i}", "expires_at": "2026-10-03T11:34:09Z"}
                for i, m in enumerate(messages)
            ],
            "active_leases": {"acquired_now_count": len(messages), "already_held_count": 0,
                              "total_held_count": len(messages)},
            "items": [{"message": m, "project_ref": PROJECT} for m in messages],
        },
    }


def _lease_read(message):
    return {
        "ok": True,
        "result": {
            "message": {**message, "lease": {"lease_id": LEASE, "expires_at": "2026-10-03T11:34:09Z"}},
            "project_ref": PROJECT,
            "settlement": {"required": True, "outcomes": ["acknowledged", "refused"]},
        },
    }


def _attachment(filename, *, sha="a" * 64, **overrides):
    file_ref = f"pbfile:owner/session/command_x-file-0-{sha}/{filename}"
    attachment = {
        "schema": "problem-board.local-mail-attachment.v1",
        "filename": filename,
        "mime": "text/markdown",
        "size": 3110,
        "file_ref": file_ref,
        "sha256": sha,
        "read_command": [
            "pb", "worker", "attachment-read", *FLAGS, "--project-ref", PROJECT,
            "--message-ref", MESSAGE_REF, "--lease-id", LEASE, "--file-ref", file_ref,
        ],
    }
    attachment.update(overrides)
    return attachment


def test_receive_drops_only_empty_envelope_defaults_and_keeps_the_handling_ledger():
    message = _message()
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "Keep provenance." in text and "Quote attachment commands." in text
    # The W393 handling ledger stays whole, the sender's idempotency key included.
    for kept in (MESSAGE_REF, "correlation_id: w472-start", "reply_to: ", f"idempotency_key: {IDEMPOTENCY}",
                 COMMAND_REF, SOURCE_REF, "payload_hash = 1250584", f"lease_id: {LEASE}0",
                 "lease_expires_at: "):
        assert kept in text, kept
    for dropped in ("identity_ref =", "operator_origin.channel", "operator_origin.ref", "payload = {}",
                    "work_ref = \n", "  work_ref = "):
        assert dropped not in text, dropped
    assert "settle: pb worker settle" in text and f"--lease-id {LEASE}0" in text


def test_lease_read_and_json_keep_the_complete_message():
    message = _message()
    full = render_envelope(_lease_read(message), worker_flags=FLAGS)
    assert f"idempotency_key: {IDEMPOTENCY}" in full
    for complete in ("identity_ref =", "operator_origin.channel = unknown", "payload = {}"):
        assert complete in full, complete
    raw = json.dumps(_receive(message))
    assert IDEMPOTENCY in raw and '"identity_ref": ""' in raw


def test_structured_task_and_review_fields_are_never_removed():
    assignment = _message(
        kind="assign", sender="control-plane", body="You have been assigned work.",
        work_ref=WORK_REF,
        payload={
            "assignment_ref": "work:assignment:a1",
            "ownership_version": 2,
            "expected_reaction": "begin_work",
            "item_status": "working",
            "work_ref": WORK_REF,
            "command": {"instructions": "Implement F1 to F3 only.", "review_request": {"reviewer": "ops"}},
            "identity_ref": "",
            "nested": {"identity_ref": "", "payload": {}},
        },
    )
    text = render_envelope(_receive(assignment), worker_flags=FLAGS)
    for kept in ("assignment_ref = work:assignment:a1", "ownership_version = 2",
                 "expected_reaction = begin_work", "command.instructions = Implement F1 to F3 only.",
                 "command.review_request.reviewer = ops", "nested.identity_ref = ",
                 "nested.payload = {}", f"work_ref: {WORK_REF}"):
        assert kept in text, kept
    # The duplicate top-level work_ref is the only work_ref line left out.
    assert f"  work_ref = {WORK_REF}" not in text


def test_a_distinct_payload_work_ref_and_a_named_operator_origin_stay():
    message = _message(
        work_ref=WORK_REF,
        payload={"work_ref": "work:plan:node:other", "operator_origin": {"channel": "telegram", "ref": "tg:1"},
                 "source_message_ref": MESSAGE_REF},
    )
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "work_ref = work:plan:node:other" in text
    assert "operator_origin.channel = telegram" in text and "operator_origin.ref = tg:1" in text
    # A source_message_ref equal to the header is still printed: provenance is never inferred away.
    assert f"source_message_ref = {MESSAGE_REF}" in text


@pytest.mark.parametrize("filename", [
    "verification.md", "my report (final).md", "it's \"quoted\".md", "отчёт ✓.md", "a;rm -rf x.md",
])
def test_an_attachment_is_one_summary_and_a_runnable_exact_command(filename):
    attachment = _attachment(filename)
    message = _message(attachment_count=1, attachments=[attachment])
    lines = render_envelope(_receive(message), worker_flags=FLAGS).splitlines()
    assert not any("read_command[" in line for line in lines)
    assert not any("local-mail-attachment.v1" in line for line in lines)
    summary = next(line for line in lines if line.startswith("  [1] "))
    assert filename in summary and "text/markdown" in summary and "3110 bytes" in summary
    assert f"      sha256 = {'a' * 64}" in lines
    assert f"      file_ref = {attachment['file_ref']}" in lines
    read = next(line for line in lines if line.startswith("      read: "))
    assert shlex.split(read.removeprefix("      read: ")) == attachment["read_command"]


def test_several_attachments_keep_order_and_their_own_commands():
    first = _attachment("one.md", sha="1" * 64)
    second = _attachment("two.json", sha="2" * 64, mime="application/json", size=3423)
    message = _message(attachment_count=2, attachments=[first, second])
    lines = render_envelope(_receive(message), worker_flags=FLAGS).splitlines()
    reads = [shlex.split(line.removeprefix("      read: ")) for line in lines if line.startswith("      read: ")]
    assert reads == [first["read_command"], second["read_command"]]
    assert [line for line in lines if line.startswith("  [")][1].startswith("  [2] two.json · application/json")


@pytest.mark.parametrize("broken", [
    {"read_command": None},
    {"read_command": []},
    {"read_command": ["pb", 3]},
    {"file_ref": ""},
])
def test_incomplete_attachment_metadata_is_printed_in_full_not_hidden(broken):
    attachment = _attachment("partial.md", **broken)
    message = _message(attachment_count=1, attachments=[attachment])
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "  [1] " not in text
    assert "filename = partial.md" in text and "schema = problem-board.local-mail-attachment.v1" in text


def test_an_unknown_attachment_field_and_a_foreign_schema_stay_visible():
    attachment = _attachment("x.md", schema="problem-board.future-attachment.v2", note="kept")
    message = _message(attachment_count=1, attachments=[attachment])
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "note = kept" in text and "schema = problem-board.future-attachment.v2" in text
    assert "size unknown" not in text


def test_a_replayed_duplicate_still_settles_and_keeps_its_key_in_json():
    message = _message(prior_handling={"already_handled": True, "guidance": "Settle it."})
    envelope = _receive(message, copy.deepcopy(message) | {"message_ref": MESSAGE_REF + "-again"})
    text = render_envelope(envelope, worker_flags=FLAGS)
    assert text.count("settle: pb worker settle") == 2
    assert f"--lease-id {LEASE}0" in text and f"--lease-id {LEASE}1" in text
    assert json.dumps(envelope).count(IDEMPOTENCY) == 2


def _item_block(message):
    """The one message as receive prints it, and as lease-read prints it."""
    compact = render_envelope(_receive(message), worker_flags=FLAGS).split("--- item 1 of 1\n", 1)[1]
    full = render_envelope(_lease_read(message), worker_flags=FLAGS).split("OK\n", 1)[1]
    full = "".join(line + "\n" for line in full.splitlines() if not line.startswith("settlement:"))
    return compact, full


def test_the_compact_view_is_materially_smaller_on_a_routed_item_with_attachments():
    # Measured on these fixtures (2026-10-03): a routed item with no
    # attachment is 7% smaller (23 lines, was 28); with two attachments 23%
    # smaller and 32 lines instead of 71.
    plain, plain_full = _item_block(_message())
    assert len(plain.encode()) < 0.95 * len(plain_full.encode())
    assert len(plain.splitlines()) < len(plain_full.splitlines())
    message = _message(attachment_count=2, attachments=[_attachment("one.md"), _attachment("two.md")])
    compact, full = _item_block(message)
    assert len(compact.encode()) < 0.8 * len(full.encode()), (len(compact.encode()), len(full.encode()))
    assert len(compact.splitlines()) < 0.5 * len(full.splitlines())

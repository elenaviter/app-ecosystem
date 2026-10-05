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


# W472 ownership 4: since W474 a routed mail carries the command the server
# admitted, as ``retirement_command``. Its ``mail`` fields repeat the message.


def _routed(**mail_overrides):
    """A routed mail exactly as the client stores it: header, payload and admitted copy."""
    message = _message(kind="update", correlation_id="w455-procedure-review-and-adoption", reply_to="")
    mail = {
        "kind": message["kind"],
        "subject": message["subject"],
        "body": message["body"],
        "correlation_id": message["correlation_id"],
        "reply_to": "",
        "source_message_ref": SOURCE_REF,
        "work_ref": "",
        "identity_ref": "",
        "payload": {},
    }
    mail.update(mail_overrides)
    message["payload"] = {**message["payload"], "retirement_command": {"mail": mail}}
    return message


def test_a_matching_admitted_copy_is_one_summary_line_in_receive():
    message = _routed()
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "retirement_command.mail." not in text
    assert (
        "  retirement_command = admitted copy of this message; matches its kind, subject, body, correlation_id, "
        "reply_to, source_message_ref, work_ref, identity_ref, payload (full copy: pb worker lease-read or --format json)"
    ) in text
    # The handling ledger and every distinct ref stay.
    for line in (f"message_ref: {MESSAGE_REF}", "correlation_id: w455-procedure-review-and-adoption",
                 f"idempotency_key: {IDEMPOTENCY}", f"  command_ref = {COMMAND_REF}",
                 f"  source_message_ref = {SOURCE_REF}", "settle: pb worker settle"):
        assert line in text, line
    assert "    Keep provenance.\n    Quote attachment commands." in text


def test_lease_read_and_json_keep_the_whole_admitted_copy():
    message = _routed()
    full = render_envelope(_lease_read(message), worker_flags=FLAGS)
    assert "  retirement_command.mail.subject = W472 design feedback" in full
    assert f"  retirement_command.mail.source_message_ref = {SOURCE_REF}" in full
    original = copy.deepcopy(message)
    rendered = render_envelope(_receive(message), worker_flags=FLAGS)
    assert message == original, "rendering must not change the message"
    assert json.loads(json.dumps(_receive(message)))["result"]["items"][0]["message"]["payload"]["retirement_command"] == (
        original["payload"]["retirement_command"]
    )
    assert rendered != full


@pytest.mark.parametrize("field, value", [
    ("subject", "A different subject"),
    ("body", "A different instruction that must stay visible."),
    ("correlation_id", "another-thread"),
    ("reply_to", "work:mail:20261003T100000Z:mail_9:earlier"),
    ("source_message_ref", "work:mail:20261003T100000Z:mail_8:other-source"),
    ("work_ref", "work:plan:node:20261001T000000Z:w999:unrelated"),
    ("payload", {"task": {"instructions": "Unique task text", "attempt": 0, "dry_run": False}}),
])
def test_a_divergent_field_is_printed_in_full_beside_the_summary(field, value):
    text = render_envelope(_receive(_routed(**{field: value})), worker_flags=FLAGS)
    assert "  retirement_command.mail.(matching fields) = admitted copy of this message; matches its" in text
    if isinstance(value, dict):
        assert "  retirement_command.mail.payload.task.instructions = Unique task text" in text
        assert "  retirement_command.mail.payload.task.attempt = 0" in text
        assert "  retirement_command.mail.payload.task.dry_run = False" in text
    else:
        assert f"  retirement_command.mail.{field} = {value}" in text


def test_an_unknown_field_or_shape_is_never_folded_away():
    extra = render_envelope(_receive(_routed(attachments=[{"filename": "plan.md"}], priority="urgent")), worker_flags=FLAGS)
    assert "  retirement_command.mail.priority = urgent" in extra
    assert "retirement_command.mail.attachments" in extra
    message = _routed()
    message["payload"]["retirement_command"] = {"mail": message["payload"]["retirement_command"]["mail"], "note": "x"}
    other_shape = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "  retirement_command.mail.subject = W472 design feedback" in other_shape
    assert "  retirement_command.note = x" in other_shape
    not_a_mapping = _routed()
    not_a_mapping["payload"]["retirement_command"] = {"mail": "opaque"}
    assert "  retirement_command.mail = opaque" in render_envelope(_receive(not_a_mapping), worker_flags=FLAGS)


def test_an_adapted_work_locator_matches_a_locator_the_message_shows():
    versioned = WORK_REF + ":20261003T120000Z-abc"
    message = _routed(work_ref=versioned, identity_ref=WORK_REF)
    message["work_ref"] = versioned
    message["payload"].update({"work_ref": WORK_REF, "identity_ref": WORK_REF})
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "retirement_command.mail." not in text
    assert f"work_ref: {versioned}" in text


def test_an_uncorrelated_mail_matches_its_command_ref():
    message = _routed(correlation_id="")
    message["correlation_id"] = COMMAND_REF
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "retirement_command.mail." not in text


def test_folding_the_admitted_copy_shrinks_a_routed_item():
    # Measured on this fixture (2026-10-03): 9 copy lines become 1.
    plain = render_envelope(_receive(_message(kind="update", correlation_id="w455-procedure-review-and-adoption",
                                              reply_to="")), worker_flags=FLAGS)
    routed = render_envelope(_receive(_routed()), worker_flags=FLAGS)
    unfolded = render_envelope(_lease_read(_routed()), worker_flags=FLAGS)
    assert routed.count("\n") == plain.count("\n") + 1
    assert unfolded.count("retirement_command.mail.") == 9


# Review return (CodeApp, 13:41Z): Python treats 0 == False and 1 == True,
# a malformed locator crashed the renderer, and falsey reply_to values were
# taken for an empty one.


@pytest.mark.parametrize("delivered, admitted", [(0, False), (False, 0), (1, True), (True, 1)])
def test_a_nested_type_change_is_never_folded(delivered, admitted):
    message = _routed(payload={"task": {"value": admitted}})
    message["payload"]["payload"] = {"task": {"value": delivered}}
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert f"  retirement_command.mail.payload.task.value = {admitted}" in text


def test_identical_nested_zero_and_false_still_fold():
    inner = {"task": {"instructions": "Run it", "attempt": 0, "dry_run": False}}
    message = _routed(payload=inner)
    message["payload"]["payload"] = copy.deepcopy(inner)
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "retirement_command.mail." not in text
    assert "  payload.task.attempt = 0" in text
    assert "  payload.task.dry_run = False" in text


@pytest.mark.parametrize("malformed", [[], {}, 7, None])
def test_a_malformed_locator_neither_crashes_nor_matches(malformed):
    message = _routed(work_ref=WORK_REF + ":v2")
    message["payload"]["versioned_work_ref"] = malformed
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert f"  retirement_command.mail.work_ref = {WORK_REF}:v2" in text
    assert "settle: pb worker settle" in text


@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_a_falsey_reply_to_is_not_an_empty_one(value):
    text = render_envelope(_receive(_routed(reply_to=value)), worker_flags=FLAGS)
    assert "  retirement_command.mail.(matching fields) = admitted copy of this message;" in text
    assert "retirement_command.mail.reply_to" in text


@pytest.mark.parametrize("field, value", [("kind", 1), ("subject", None), ("source_message_ref", True),
                                          ("identity_ref", 0), ("correlation_id", False)])
def test_a_field_of_an_unexpected_type_is_printed(field, value):
    text = render_envelope(_receive(_routed(**{field: value})), worker_flags=FLAGS)
    assert f"retirement_command.mail.{field}" in text


# W472 ownership 7: an attachment-bearing routed mail carries the admitted
# attachment entries, each with a short-lived signed download_url. Brief output
# never prints a signed link (Infra 2026-10-03), and entries that repeat the
# message's own attachment fold into one line.

SIGNED = "https://board.example/api/files/abc?sig=SECRETSIG123&expires=1791030000&token=TKN456"


def _admitted_entry(local, **overrides):
    entry = {
        "filename": local["filename"], "mime": local["mime"], "size": local["size"],
        "sha256": local["sha256"], "file_ref": local["file_ref"],
        "stored_name": "stored.md", "owner_id": "owner", "conversation_id": "conv", "turn_id": "turn",
        "download_path": "/resources/owner/conv/turn/stored.md", "download_url": SIGNED,
    }
    entry.update(overrides)
    return entry


def _routed_with_attachment(**entry_overrides):
    local = _attachment("plan.md")
    message = _routed()
    message["attachment_count"] = 1
    message["attachments"] = [local]
    message["payload"]["retirement_command"]["attachments"] = [_admitted_entry(local, **entry_overrides)]
    message["payload"]["retirement_command"]["attachment_request_hash"] = "f" * 64
    return message


def _assert_no_secret(text):
    for secret in ("SECRETSIG123", "TKN456", "sig=", "token="):
        assert secret not in text, secret


def test_a_signed_download_link_never_reaches_brief_output():
    message = _routed_with_attachment()
    receive = render_envelope(_receive(message), worker_flags=FLAGS)
    full = render_envelope(_lease_read(message), worker_flags=FLAGS)
    _assert_no_secret(receive)
    _assert_no_secret(full)
    assert "retirement_command.attachments[0].download_url = (signed link withheld; use the attachment read command)" in full
    # The stored message and the JSON view are unchanged, for diagnosis.
    assert message["payload"]["retirement_command"]["attachments"][0]["download_url"] == SIGNED


def test_matching_admitted_attachment_entries_fold_to_one_line():
    text = render_envelope(_receive(_routed_with_attachment()), worker_flags=FLAGS)
    assert (
        "  retirement_command.attachments = 1 admitted attachment entry match this message's attachments "
        "by file_ref, filename, mime, size and sha256"
    ) in text
    assert "retirement_command.attachments[0]" not in text
    assert "  retirement_command.mail = admitted copy of this message; matches its" in text
    assert "  retirement_command.attachment_request_hash = " + "f" * 64 in text
    # The message's own attachment summary and exact read command stay.
    assert "  [1] plan.md · text/markdown · 3110 bytes" in text
    assert "read: pb worker attachment-read" in text


@pytest.mark.parametrize("overrides", [
    {"sha256": "b" * 64},
    {"size": 9},
    {"file_ref": "pbfile:owner/session/other/plan.md"},
    {"priority": "urgent"},
    {"size": True},
])
def test_a_divergent_or_unknown_attachment_entry_prints_in_full_without_the_link(overrides):
    text = render_envelope(_receive(_routed_with_attachment(**overrides)), worker_flags=FLAGS)
    assert "retirement_command.attachments[0].file_ref = " in text
    assert "retirement_command.attachments[0].download_url = (signed link withheld; use the attachment read command)" in text
    _assert_no_secret(text)


def test_a_signed_query_anywhere_in_a_payload_is_withheld_but_plain_links_stay():
    message = _message(payload={
        "command_ref": COMMAND_REF,
        "evidence": {"artifact": SIGNED, "pr": "https://github.com/elenaviter/app-ecosystem/pull/465"},
        "links": ["https://cdn.example/f.png?X-Amz-Signature=abc&X-Amz-Credential=def", "https://example.org/a?page=2"],
    })
    for envelope in (_receive(message), _lease_read(message)):
        text = render_envelope(envelope, worker_flags=FLAGS)
        _assert_no_secret(text)
        assert "X-Amz-Signature" not in text and "abc" not in text.split("links[0] = ", 1)[1].split("\n", 1)[0]
        assert "  evidence.artifact = https://board.example/api/files/abc?(signed query withheld: 3 parameters)" in text
        assert "  evidence.pr = https://github.com/elenaviter/app-ecosystem/pull/465" in text
        assert "  links[1] = https://example.org/a?page=2" in text


def _routed_with_attachments(entries_overrides):
    locals_ = [_attachment(f"part-{i}.md", sha=chr(ord("a") + i) * 64) for i in range(len(entries_overrides))]
    message = _routed()
    message["attachment_count"] = len(locals_)
    message["attachments"] = locals_
    message["payload"]["retirement_command"]["attachments"] = [
        _admitted_entry(local, **overrides) for local, overrides in zip(locals_, entries_overrides)
    ]
    return message


def test_two_matching_attachment_entries_fold_and_keep_both_read_commands():
    text = render_envelope(_receive(_routed_with_attachments([{}, {}])), worker_flags=FLAGS)
    assert "  retirement_command.attachments = 2 admitted attachment entries match this message's attachments" in text
    assert "  [1] part-0.md · text/markdown · 3110 bytes" in text
    assert "  [2] part-1.md · text/markdown · 3110 bytes" in text
    assert text.count("read: pb worker attachment-read") == 2
    _assert_no_secret(text)


def test_one_divergent_entry_among_several_prints_every_entry_in_full():
    text = render_envelope(_receive(_routed_with_attachments([{}, {"sha256": "0" * 64}])), worker_flags=FLAGS)
    assert "retirement_command.attachments = " not in text
    assert "retirement_command.attachments[0].sha256 = " + "a" * 64 in text
    assert "retirement_command.attachments[1].sha256 = " + "0" * 64 in text
    _assert_no_secret(text)


@pytest.mark.parametrize("file_ref", [None, 7, ["pbfile:x"], {"ref": "x"}, ""])
def test_a_malformed_entry_file_ref_neither_crashes_nor_folds(file_ref):
    text = render_envelope(_receive(_routed_with_attachment(file_ref=file_ref)), worker_flags=FLAGS)
    assert "retirement_command.attachments[0]" in text
    assert "settle: pb worker settle" in text
    _assert_no_secret(text)


def test_the_delivery_doc_states_the_brief_rules_and_the_evidence_path():
    from pathlib import Path

    doc = " ".join(
        (Path(__file__).resolve().parents[3] / "docs" / "delivery.md").read_text(encoding="utf-8").split()
    )
    assert "## What an agent reads, and how evidence travels" in doc
    assert "(signed link withheld; use the attachment read command)" in doc
    assert "(signed query withheld: N parameters)" in doc
    assert "Values compare as JSON, so `0` and `false` never match." in doc
    assert "goes as a file with `pb worker send --attach <file>`" in doc


# Review return (CodeApp, 16:05Z) on 7eea60e7: links in prose and headers,
# signed values under unexpected shapes, a parser-rejected link, and folding
# that hid locator content or claimed identity fields an entry lacked.

PROSE_SECRET = "https://files.example.test/report?token=SYNTHETIC_REVIEW_SECRET&expires=123"


def _both_views(message):
    return [render_envelope(_receive(message), worker_flags=FLAGS),
            render_envelope(_lease_read(message), worker_flags=FLAGS)]


@pytest.mark.parametrize("placement", ["body", "subject", "prose", "download_url_mapping", "download_url_nested"])
def test_a_signed_value_is_withheld_in_every_placement(placement):
    message = _message()
    if placement == "body":
        message["body"] = f"Evidence: {PROSE_SECRET}\nSecond line."
    elif placement == "subject":
        message["subject"] = f"Report at {PROSE_SECRET}"
    elif placement == "prose":
        message["payload"]["evidence"] = f"See {PROSE_SECRET} for the run."
    elif placement == "download_url_mapping":
        message["payload"]["download_url"] = {"opaque": "SYNTHETIC_REVIEW_SECRET"}
    else:
        message["payload"]["download_url"] = [["SYNTHETIC_REVIEW_SECRET"]]
    for text in _both_views(message):
        assert "SYNTHETIC_REVIEW_SECRET" not in text
        assert "settle: pb worker settle" in text
    if placement in ("body", "subject", "prose"):
        assert "https://files.example.test/report?(signed query withheld: 2 parameters)" in _both_views(message)[0]


def test_a_parser_rejected_link_never_stops_the_view_and_is_still_withheld():
    message = _message()
    message["payload"]["evidence"] = "https://[broken-host/report?token=SYNTHETIC_REVIEW_SECRET"
    for text in _both_views(message):
        assert "SYNTHETIC_REVIEW_SECRET" not in text
        assert "https://[broken-host/report?(signed query withheld: 1 parameters)" in text
        assert "settle: pb worker settle" in text


def test_a_token_in_a_link_fragment_is_withheld_and_ordinary_fragments_stay():
    message = _message()
    message["payload"]["callback"] = "https://app.example/cb#access_token=SYNTHETIC_REVIEW_SECRET&state=1"
    message["payload"]["doc"] = "https://docs.example/page#section-2"
    for text in _both_views(message):
        assert "SYNTHETIC_REVIEW_SECRET" not in text
        assert "https://docs.example/page#section-2" in text


@pytest.mark.parametrize("missing", ["filename", "mime", "size", "sha256"])
def test_an_entry_missing_an_identity_field_is_not_claimed_to_match(missing):
    message = _routed_with_attachment()
    del message["payload"]["retirement_command"]["attachments"][0][missing]
    text = render_envelope(_receive(message), worker_flags=FLAGS)
    assert "admitted attachment entry match" not in text
    assert "retirement_command.attachments[0].file_ref = " in text
    _assert_no_secret(text)


@pytest.mark.parametrize("locator", ["owner_id", "download_path", "stored_name", "conversation_id", "turn_id"])
def test_a_locator_with_nested_content_is_printed_not_folded(locator):
    text = render_envelope(
        _receive(_routed_with_attachment(**{locator: {"action": "VISIBLE_NEXT_ACTION"}})), worker_flags=FLAGS,
    )
    assert "admitted attachment entry match" not in text
    assert f"retirement_command.attachments[0].{locator}.action = VISIBLE_NEXT_ACTION" in text
    _assert_no_secret(text)


def test_an_admitted_copy_with_attachment_custody_still_folds_without_locators():
    # W563: the server added `attachment_custody` beside the admitted copy's
    # attachments; the unknown key stopped the fold, so every routed message
    # with a file printed the whole copy, download paths included.
    attachment = _attachment("w563-red-main.xml")
    message = _routed()
    message["attachment_count"] = 1
    message["attachments"] = [attachment]
    admitted = {
        key: attachment[key] for key in ("filename", "mime", "size", "sha256", "file_ref")
    }
    admitted.update(download_path="/api/cb/resources/owner/session/attachment/w563-red-main.xml/download",
                    stored_name="w563-red-main.xml", owner_id="owner", conversation_id="session", turn_id="turn")
    message["payload"]["retirement_command"].update(
        attachments=[admitted], attachment_request_hash="b" * 64, attachment_custody="delivery",
    )

    text = render_envelope(_receive(message), worker_flags=FLAGS)

    assert "retirement_command.mail." not in text
    assert "download_path" not in text and "/download" not in text
    assert "1 admitted attachment entry match this message's attachments" in text
    assert "retirement_command.attachment_custody = delivery" in text

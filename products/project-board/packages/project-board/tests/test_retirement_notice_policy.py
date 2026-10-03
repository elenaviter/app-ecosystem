"""Synthetic terminal-notice and retirement batching regression controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.client.io import atomic_write_json, read_json
from project_board.client.mail_delivery import archive_mailbox_messages
from project_board.client.store import SharedFieldStore
from project_board.contract.delivery_failures import is_terminal_system_notice
from project_board.contract.errors import DomainError


def test_retirement_publication_is_durable_pending_until_exact_notice_coverage(tmp_path):
    from project_board.client.mail_delivery import retirement_delivery_publication
    from project_board.client.outbox_store import OutboxStore
    field = SimpleNamespace(control=tmp_path / 'control')
    field._outbox = OutboxStore(field.control)
    original = {'message_ref': 'work:mail:original', 'recipient': 'codex-retired',
                'kind': 'question', 'subject': 'Private', 'body': 'Retained original body',
                'sender': 'codex-author', 'sender_identity': {'kind': 'worker', 'worker_name': 'codex-author'}}
    kwargs = dict(project_ref='work:project:alpha', reporter_worker_name='codex-author',
                  retired_worker_name='codex-retired', message=original)
    first = retirement_delivery_publication(field, **kwargs)
    assert first['delivery_status'] == 'pending'
    replay = retirement_delivery_publication(field, **kwargs)
    assert replay['outbox_id'] == first['outbox_id']
    row = field._outbox.read(first['outbox_id'], worker_name='codex-author', project_ref='work:project:alpha')
    assert row['payload']['purpose'] == 'retired_worker_delivery'
    # A successful transport status, or another original's coverage, is not
    # proof that this retained original may leave pending history.
    row.update(state='sent', remote_result={'coverage': [{'source_message_ref': 'work:mail:other',
        'notice_state': 'queued', 'receipt_ref': 'work:mail_reconciliation:other'}]})
    field._outbox.write_pending(row)
    assert retirement_delivery_publication(field, **kwargs)['delivery_status'] == 'pending'
    row['remote_result'] = {'schema': 'problem-board.retirement-delivery.v1',
                           'coverage': [{'source_message_ref': 'work:mail:original',
                           'notice_state': 'queued', 'receipt_ref': 'work:mail_reconciliation:canonical'}]}
    field._outbox.write_pending(row)
    assert retirement_delivery_publication(field, **kwargs)['delivery_status'] == 'queued'
    assert original['body'] == 'Retained original body'


def test_legacy_routed_original_exposes_incomplete_proof_without_false_conflict(tmp_path):
    from project_board.client.mail_delivery import retirement_delivery_publication
    from project_board.client.outbox_store import OutboxStore
    from project_board.client.io import content_hash
    field = SimpleNamespace(control=tmp_path / 'control')
    field._outbox = OutboxStore(field.control)
    command = {'mail': {'kind': 'question', 'source_message_ref': 'work:mail:legacy',
                       'subject': 'Private', 'body': 'Private body', 'payload': {},
                       'correlation_id': 'thread', 'reply_to': 'work:mail:question',
                       'attachments': [{'filename': 'original.txt', 'sha256': '0' * 64}]}}
    original = {'message_ref': 'work:mail:local', 'kind': 'question', 'subject': 'Private',
                'body': 'Private body', 'correlation_id': 'thread', 'reply_to': 'work:mail:question',
                'payload': {'command_ref': 'work:control:legacy', 'source_message_ref': 'work:mail:legacy',
                            'payload_hash': content_hash(command), 'payload': {}}}
    result = retirement_delivery_publication(field, project_ref='work:project:alpha',
        reporter_worker_name='codex-reporter', retired_worker_name='codex-retired', message=original)
    assert result['delivery_status'] == 'pending'
    assert result['reason'] == 'legacy_original_proof_incomplete'
    assert 'Private' not in str(result)
    assert list(field._outbox.in_flight('pending')) == []


@pytest.mark.parametrize('code,reason', [
    ('work_retirement_generation_pending', ''),
    ('work_retirement_evidence_pending', ''),
    ('work_retirement_evidence_pending', 'canonical_server_member_exists_unverifiable_author_copy'),
])
def test_retirement_pending_admission_retries_same_evidence_without_refusal(code, reason):
    publication = {'schema': 'problem-board.retirement-delivery.v1',
                   'purpose': 'retired_worker_delivery', 'retired_worker_name': 'codex-retired',
                   'members': [{'source_message_ref': 'work:mail:retained-original'}]}

    class Field:
        def __init__(self):
            self.claimed = False
            self.retries = []

        def pull_outbox(self, **kwargs):
            if self.claimed:
                return []
            self.claimed = True
            return [{'outbox_id': 'same-evidence', 'kind': 'mail.reconciliation.publish',
                     'worker_name': 'codex-author', 'project_ref': 'work:project:alpha', 'payload': publication}]

        def retry_outbox(self, outbox_id, **kwargs):
            self.retries.append((outbox_id, kwargs['error_code']))

        def settle_outbox(self, *_args, **_kwargs):
            raise AssertionError('Pending evidence cannot be terminally refused or covered.')

    class Client:
        async def action(self, **kwargs):
            assert kwargs['payload'] == publication
            raise DomainError(code, 'Canonical evidence is not yet admitted.', status=409,
                              details={'reason': reason} if reason else {})

    field = Field()
    adapter = relay.ProblemBoardHostRelayAdapter.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(relay_id='synthetic-relay', worker_name='codex-author',
                                     project_id='alpha', connection_hub_profile='synthetic-profile')
    adapter.field, adapter.client, adapter._outbox_finishes = field, Client(), set()
    counts = asyncio.run(adapter._flush_outbox_unlocked())
    assert counts['outbox_retried'] == 1 and counts['outbox_refused'] == 0
    assert field.retries == [('same-evidence', reason or code)]


def test_unverifiable_author_copy_retains_distinct_pending_reason_and_original(tmp_path):
    from project_board.client.mail_delivery import retirement_delivery_publication
    from project_board.client.outbox_store import OutboxStore
    field = SimpleNamespace(control=tmp_path / 'control')
    field._outbox = OutboxStore(field.control)
    original = {'message_ref': 'work:mail:lost-route', 'kind': 'question', 'subject': 'Private',
                'body': 'Retain exact original', 'sender': 'codex-author'}
    kwargs = dict(project_ref='work:project:alpha', reporter_worker_name='codex-author',
                  retired_worker_name='codex-retired', message=original)
    first = retirement_delivery_publication(field, **kwargs)
    row = field._outbox.read(first['outbox_id'], worker_name='codex-author', project_ref='work:project:alpha')
    row['last_error_code'] = 'canonical_server_member_exists_unverifiable_author_copy'
    field._outbox.write_pending(row)
    replay = retirement_delivery_publication(field, **kwargs)
    assert replay['delivery_status'] == 'pending'
    assert replay['reason'] == row['last_error_code']
    assert replay['outbox_id'] == first['outbox_id']
    assert 'coverage' not in replay and 'Private' not in str(replay)
    assert original['body'] == 'Retain exact original'



def test_retirement_mixed_batch_marks_only_exact_unverifiable_original_pending(tmp_path):
    from project_board.client.mail_delivery import retirement_delivery_publication
    from project_board.client.outbox_store import OutboxStore
    field = SimpleNamespace(control=tmp_path / "control")
    field._outbox = OutboxStore(field.control)
    messages = [{"message_ref": f"work:mail:{ref}", "kind": "question", "subject": "Private",
                 "body": f"Retain private {ref}", "sender": "codex-author"}
                for ref in ("ordinary-0", "overlap", "ordinary-1")]
    kwargs = dict(project_ref="work:project:alpha", reporter_worker_name="codex-author",
                  retired_worker_name="codex-retired")
    first = retirement_delivery_publication(field, **kwargs, message=messages[0], additional_messages=messages[1:])
    bindings = {entry["source_message_ref"]: entry for entry in first["publication_bindings"]}
    for message in messages:
        entry = bindings[message["message_ref"]]
        message["retirement_publication"] = {"outbox_id": first["outbox_id"], "proof_hash": entry["proof_hash"]}
    row = field._outbox.read(first["outbox_id"], worker_name="codex-author", project_ref="work:project:alpha")
    row.update(state="sent", remote_result={
        "schema": "problem-board.retirement-delivery.v1",
        "coverage": [{"source_message_ref": message["message_ref"], "notice_state": "queued",
                      "receipt_ref": "work:mail_reconciliation:canonical"} for message in (messages[0], messages[2])],
        "pending_refs": ["work:mail:overlap"],
        "pending": [{"source_message_ref": "work:mail:overlap",
                     "reason": "canonical_server_member_exists_unverifiable_author_copy"}]})
    field._outbox.write_pending(row)
    for _ in range(2):  # repeat passes consume the same durable response, not another publication
        results = [retirement_delivery_publication(field, **kwargs, message=message) for message in messages]
        assert [result["delivery_status"] for result in results] == ["queued", "pending", "queued"]
        assert results[1]["reason"] == "canonical_server_member_exists_unverifiable_author_copy"
        assert all(result["outbox_id"] == first["outbox_id"] for result in results)
        assert "coverage" not in results[1]
        assert all("Private" not in str(result) for result in results)
    assert [message["body"] for message in messages] == [
        "Retain private ordinary-0", "Retain private overlap", "Retain private ordinary-1"]



@pytest.mark.parametrize("state,schema,pending_ref,reason,covered,expected_status,expected_reason", [
    ("sent", "problem-board.retirement-delivery.v1", "work:mail:original",
     "canonical_server_member_exists_unverifiable_author_copy", True, "pending",
     "canonical_server_member_exists_unverifiable_author_copy"),
    ("sent", "problem-board.retirement-delivery.v1", "work:mail:original",
     "unrecognized-private-value", True, "pending", "canonical_notice_coverage_pending"),
    ("sent", "problem-board.retirement-delivery.v1", "work:mail:other",
     "canonical_server_member_exists_unverifiable_author_copy", True, "queued", ""),
    ("sent", "wrong-schema", "work:mail:original",
     "canonical_server_member_exists_unverifiable_author_copy", True, "pending", "canonical_notice_coverage_pending"),
    ("pending", "problem-board.retirement-delivery.v1", "work:mail:original",
     "canonical_server_member_exists_unverifiable_author_copy", True, "pending", "canonical_notice_coverage_pending"),
    ("sent", "problem-board.retirement-delivery.v1", "",
     "", False, "pending", "canonical_notice_coverage_pending"),
])
def test_retirement_pending_result_is_exact_ref_sent_schema_and_whitelisted(
        tmp_path, state, schema, pending_ref, reason, covered, expected_status, expected_reason):
    from project_board.client.mail_delivery import retirement_delivery_publication
    from project_board.client.outbox_store import OutboxStore
    field = SimpleNamespace(control=tmp_path / "control")
    field._outbox = OutboxStore(field.control)
    original = {"message_ref": "work:mail:original", "kind": "question", "body": "Private retained body",
                "sender": "codex-author"}
    kwargs = dict(project_ref="work:project:alpha", reporter_worker_name="codex-author",
                  retired_worker_name="codex-retired", message=original)
    first = retirement_delivery_publication(field, **kwargs)
    row = field._outbox.read(first["outbox_id"], worker_name="codex-author", project_ref="work:project:alpha")
    row.update(state=state, remote_result={"schema": schema,
        "pending_refs": [pending_ref] if pending_ref else [],
        "pending": [{"source_message_ref": pending_ref, "reason": reason}] if pending_ref else [],
        "coverage": [{"source_message_ref": "work:mail:original", "notice_state": "queued",
                      "receipt_ref": "work:mail_reconciliation:canonical"}] if covered else []})
    if state == "sent":
        row["last_error_code"] = "canonical_server_member_exists_unverifiable_author_copy"
    field._outbox.write_pending(row)
    result = retirement_delivery_publication(field, **kwargs)
    assert result["delivery_status"] == expected_status
    assert result.get("reason", "") == expected_reason
    assert "unrecognized-private-value" not in str(result) and "Private" not in str(result)
    assert original["body"] == "Private retained body"


def test_canonical_retirement_control_proof_survives_erased_payload_and_rejects_spoof():
    from project_board.contract.delivery_failures import retirement_control_member
    import hashlib
    import json
    command = {'mail': {'kind': 'question', 'source_message_ref': 'work:mail:original',
                        'subject': 'Private original', 'body': 'Must not enter metadata'}}
    digest = hashlib.sha256(json.dumps(command, ensure_ascii=True, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    control = {'command_ref': 'work:control:original', 'project_ref': 'work:project:alpha',
               'kind': 'mail', 'recipient_worker_id': 'retired-id',
               'sender_kind': 'worker', 'sender_worker_id': 'immutable-author',
               'sender_principal_key': 'real-card-author', 'payload_hash': digest, 'payload': {}}
    member = retirement_control_member(control, command_payload=command)
    assert member['sender_id'] == 'immutable-author'
    assert member['source_message_ref'] == 'work:mail:original'
    assert member['original_hash'] == digest
    assert 'body' not in member and 'payload' not in member
    with pytest.raises(DomainError, match='immutable'):
        retirement_control_member(control, command_payload={'mail': {**command['mail'], 'source_message_ref': 'work:mail:forged'}})
    person = retirement_control_member({**control, 'kind': 'request', 'sender_kind': 'user',
                                       'sender_worker_id': '', 'sender_principal_key': 'user:canonical-person'})
    assert person['sender_kind'] == 'person'
    assert person['sender_id'] == 'user:canonical-person'
    assert person['source_message_ref'] == control['command_ref']


@pytest.mark.parametrize("message,terminal", [
    ({"kind": "delivery_failed", "sender_identity": {"kind": "worker"}}, True),
    ({"kind": "discard.notice", "sender_kind": "service"}, True),
    ({"kind": "discard.notice", "sender_identity": {"kind": "worker"}}, False),
    ({"kind": "request", "sender_kind": "service"}, False),
    ({"kind": "request", "payload": {"mail": {"kind": "delivery_failed"}}}, False),
    ({"kind": "mail", "payload": {"mail": {"kind": "delivery_failed"}}}, True),
    ({"kind": "mail", "payload": {"mail": {"kind": "discard.notice"}},
      "sender_kind": "service"}, True),
    ({"kind": "mail", "payload": {"mail": {"kind": "request",
      "payload": {"kind": "delivery_failed", "sender_kind": "service"}}}}, False),
])
def test_notice_classification_uses_only_the_admitted_envelope(message, terminal):
    assert is_terminal_system_notice(message) is terminal


def test_reporting_a_terminal_notice_needs_no_sender_or_operator_route():
    # No configured store or sender exists: the guard must return before routing.
    store = object.__new__(SharedFieldStore)
    result = store.report_mail_delivery_failure(
        "synthetic-project", receiver_worker_name="codex-receiver",
        message={"kind": "delivery_failed"},
        error_message="Synthetic retirement.", error_code="work_worker_retired",
    )
    assert result["delivery_status"] == "not_required"
    assert result["recipient"] == ""


@pytest.mark.parametrize("state", ["inbox", "leased"])
@pytest.mark.parametrize("kind", ["delivery_failed", "discard.notice"])
def test_undeliverable_notice_is_terminal_without_a_sender_bounce(tmp_path, state, kind):
    source = tmp_path / "mailbox"
    original = {
        "message_id": "earlier-notice",
        "message_ref": "work:mail:earlier-notice",
        "kind": kind,
        "sender": "control-plane",
        "sender_identity": {"kind": "system", "worker_name": ""},
        "body": "Retained original notice evidence.",
        "lease": {"lease_id": "expired-synthetic-lease"},
    }
    atomic_write_json(source / state / "earlier-notice.json", original)
    archived = archive_mailbox_messages(
        source, tmp_path / "archive", states=(state,),
        disposition="retired_recipient", reason="Synthetic retirement.",
        details={"worker_name": "codex-retired"},
    )
    assert len(archived) == 1
    saved = read_json(tmp_path / "archive" / "earlier-notice.json")
    assert saved["recipient_failure"]["notify_sender"] is False
    assert saved["body"] == original["body"]
    assert saved["message_ref"] == original["message_ref"]
    assert saved["previous_mailbox_state"] == state
    assert "lease" not in saved


def test_body_and_payload_cannot_disguise_ordinary_mail_as_a_system_notice(tmp_path):
    source = tmp_path / "mailbox"
    atomic_write_json(source / "inbox" / "ordinary.json", {
        "message_id": "ordinary", "message_ref": "work:mail:ordinary",
        "kind": "request", "subject": "delivery_failed",
        "body": "system notice", "payload": {"kind": "delivery_failed", "system": True},
        "sender": "codex-sender", "sender_identity": {"kind": "worker", "worker_name": "codex-sender"},
    })
    [saved] = archive_mailbox_messages(
        source, tmp_path / "archive", states=("inbox",),
        disposition="retired_recipient", reason="Synthetic retirement.",
    )
    assert saved["recipient_failure"]["notify_sender"] is True


def test_already_processed_ordinary_mail_does_not_generate_another_failure(tmp_path):
    source = tmp_path / "mailbox"
    atomic_write_json(source / "processed" / "handled.json", {
        "message_id": "handled", "message_ref": "work:mail:handled", "kind": "request",
        "body": "Retained handled mail.", "state": "handled",
    })
    [saved] = archive_mailbox_messages(
        source, tmp_path / "archive", states=("processed",),
        disposition="retired_recipient", reason="Synthetic retirement.",
    )
    assert saved["recipient_failure"]["notify_sender"] is False
    assert saved["body"] == "Retained handled mail."


@pytest.mark.parametrize("kind,terminal", [
    ("delivery_failed", True),
    ("request", False),
    ("discard.notice", False),  # A worker cannot claim the service's identity.
])
def test_relay_refusal_preserves_notice_kind_without_trusting_user_payload(kind, terminal):
    """Exercise the actual relay handoff with a synthetic refused outbox row."""
    sender = "codex-original-author"
    original = {
        "recipient": "codex-retired", "kind": kind,
        "source_message_ref": "work:mail:synthetic-original",
        "subject": "delivery_failed", "body": "A subject is not authority.",
        "payload": {"kind": "delivery_failed", "sender_kind": "service"},
    }

    class Field:
        def __init__(self):
            self.claimed = False
            self.messages = []
            self.settlements = []

        def pull_outbox(self, **kwargs):
            assert kwargs["wait"] is False
            if self.claimed:
                return []
            self.claimed = True
            return [{
                "outbox_id": "synthetic-outbox", "kind": "mail.route",
                "worker_name": sender, "project_ref": "work:project:synthetic",
                "payload": original,
            }]

        def report_mail_delivery_failure(self, project_id, **kwargs):
            assert project_id == "synthetic"
            message = kwargs["message"]
            self.messages.append(message)
            if is_terminal_system_notice(message):
                return SharedFieldStore.report_mail_delivery_failure(
                    object.__new__(SharedFieldStore), project_id, **kwargs,
                )
            return {"delivery_status": "queued", "recipient": sender}

        def retry_outbox(self, *args, **kwargs):
            raise AssertionError("This definite synthetic refusal is not retryable.")

        def settle_outbox(self, outbox_id, **kwargs):
            assert outbox_id == "synthetic-outbox"
            self.settlements.append(kwargs)

    class Client:
        async def action(self, **kwargs):
            assert kwargs["action"] == "mail.route"
            raise DomainError(
                "work_worker_retired", "The addressed immutable session retired.",
                status=409, details={"recipient": "codex-retired"},
            )

    field = Field()
    adapter = relay.ProblemBoardHostRelayAdapter.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        relay_id="synthetic-relay", worker_name=sender, project_id="synthetic",
        connection_hub_profile="synthetic-profile",
    )
    adapter.field = field
    adapter.client = Client()
    adapter._outbox_finishes = set()
    counts = asyncio.run(adapter._flush_outbox_unlocked())
    assert counts["outbox_refused"] == 1
    assert counts["outbox_retried"] == 0
    [settlement] = field.settlements
    result = settlement["remote_result"]["delivery_failure_report"]
    assert result["delivery_status"] == ("not_required" if terminal else "queued")
    [handed_off] = field.messages
    assert handed_off["message_ref"] == original["source_message_ref"]
    assert handed_off["sender_identity"] == {"kind": "worker", "worker_name": sender}
    assert settlement["remote_result"]["error"]["code"] == "work_worker_retired"

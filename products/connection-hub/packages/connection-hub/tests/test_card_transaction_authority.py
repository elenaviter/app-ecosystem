"""W578: the Hub stages or materializes only what a signed authority record proves (Ops 11:29)."""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards import transaction_authority as ta
from connection_hub.delegated_credentials.issuer_gate import change_digest
from test_card_service import NOW, SUBJECT_HASH, _authority

SECRET = "s" * 40
SERVICE = "problem-board"
ECHO = "0123456789abcdef" * 2
AUDIENCE = "connection-hub:tenant:project"
TX = "a" * 64
BINDING = ("project", "work:project:one")


def _record(**changes):
    before = _authority()
    candidate = dataclasses.replace(before, card_revision=before.card_revision + 1, label="narrowed").to_dict()
    record = {
        "schema": ta.RECORD_SCHEMA, "transaction_id": TX, "epoch": 1, "participant": "project",
        "binding_kind": BINDING[0], "binding_ref": BINDING[1], "actor_subject": "person", "actor_kind": "human",
        "action": "update", "subject_hash": SUBJECT_HASH, "access_id": before.access_id,
        "expected_card_revision": before.card_revision, "candidate_revision": before.card_revision + 1,
        "candidate_digest": change_digest(candidate), "expected_dependency_revisions": {"control-person": 2},
        "membership_incarnation": "m-1", "request_id": "r-1", "context_ref": "ctx", "expires_at": NOW + 60,
        "audience": AUDIENCE, "phase": "stage", "decision": "undecided", "decided_at": 0,
        "request_echo": ECHO, "candidate": candidate,
    }
    record.update(changes)
    if "intent_digest" not in changes:
        record["intent_digest"] = ta.intent_digest(record)
    return record


def _signed(record, *, secret=SECRET, service=SERVICE, now=NOW):
    return ta.sign_transaction_authority(record, secret=secret, service_id=service, now=now)


def _verify(body, *, phase="stage", now=NOW, **overrides):
    before = _authority()
    kwargs = dict(secret=SECRET, expected_service_id=SERVICE, request_echo=ECHO, audience=AUDIENCE,
                  transaction_id=TX, phase=phase, binding=BINDING, subject_hash=SUBJECT_HASH,
                  access_id=before.access_id, current_revision=before.card_revision, now=now)
    kwargs.update(overrides)
    return ta.verify_transaction_authority(body, **kwargs)


def _refused(reason, body, **kwargs):
    with pytest.raises(ta.TransactionAuthorityRefused) as caught:
        _verify(body, **kwargs)
    assert caught.value.reason == reason


def test_a_signed_undecided_unexpired_record_proves_a_stage():
    verified = _verify(_signed(_record()))
    assert verified.decision == "undecided" and verified.candidate.label == "narrowed"
    assert verified.intent_digest == ta.intent_digest(verified.record)


# ── Ops' eight required negatives ──────────────────────────────────────────


@pytest.mark.parametrize("field,value", [("label", "widened")])
def test_a_tampered_candidate_after_signing_is_refused(field, value):
    body = _signed(_record())
    body["candidate"] = {**body["candidate"], field: value}
    _refused("authority_signature_invalid", body)


def test_a_candidate_that_does_not_match_its_digest_is_refused_even_when_signed():
    record = _record()
    record["candidate"] = {**record["candidate"], "label": "widened"}  # digest left for the old candidate
    record["intent_digest"] = ta.intent_digest(record)
    _refused("card_transaction_candidate_digest_mismatch", _signed(record))


def test_an_intent_digest_that_is_not_the_recomputed_one_is_refused():
    _refused("card_transaction_intent_digest_mismatch", _signed(_record(intent_digest="c" * 64)))


@pytest.mark.parametrize("override,reason", [
    ({"audience": "connection-hub:other:project"}, "card_transaction_audience_mismatch"),
    ({"binding": ("project", "work:project:another")}, "card_transaction_binding_mismatch"),
    ({"binding": ("", "")}, "card_transaction_binding_mismatch"),
])
def test_a_wrong_audience_or_binding_is_refused(override, reason):
    _refused(reason, _signed(_record()), **override)


@pytest.mark.parametrize("mutate,reason", [
    (lambda body: body.pop("authority_proof"), "card_transaction_record_invalid"),
    (lambda body: body["authority_proof"].update(signature="x" * 43), "authority_signature_invalid"),
    (lambda body: body["authority_proof"].update(service_id="someone-else"), "authority_service_invalid"),
    (lambda body: body["authority_proof"].update(timestamp=str(NOW - 3600)), "authority_proof_stale"),
])
def test_an_unsigned_or_badly_signed_response_is_refused(mutate, reason):
    body = _signed(_record())
    mutate(body)
    _refused(reason, body)


def test_a_response_signed_with_another_secret_is_refused():
    _refused("authority_signature_invalid", _signed(_record(), secret="t" * 40))


def test_a_decision_for_another_intent_is_refused():
    body = _signed(_record(phase="decision", decision="committed", decided_at=NOW))
    _refused("card_transaction_intent_mismatch", body, phase="decision", expected_intent_digest="d" * 64)


def test_an_expired_intent_cannot_be_staged():
    _refused("card_transaction_intent_expired", _signed(_record(), now=NOW + 60), now=NOW + 60)


@pytest.mark.parametrize("decision", ["committed", "aborted"])
def test_a_stage_after_a_recorded_decision_is_refused(decision):
    _refused("card_transaction_late_stage", _signed(_record(decision=decision, decided_at=NOW)))


@pytest.mark.parametrize("decision", ["committed", "aborted"])
def test_a_recorded_decision_materializes_even_after_the_original_expiry(decision):
    later = NOW + 3600
    record = _record(phase="decision", decision=decision, decided_at=NOW + 30)
    verified = _verify(_signed(record, now=later), phase="decision", now=later,
                       expected_intent_digest=record["intent_digest"], current_revision=None)
    assert verified.decision == decision


# ── The remaining refusals, each by name ───────────────────────────────────


def test_a_replayed_response_for_another_request_is_refused():
    # The signature covers this Hub's echo, so an old response fails it first.
    _refused("authority_signature_invalid", _signed(_record(request_echo="f" * 32)))
    # And a record naming another echo is refused even when sealed over ours.
    record = _record(request_echo="f" * 32)
    seal = ta._signature(SECRET.encode(), ta._message(SERVICE, str(NOW), ECHO, record))
    body = {**record, "authority_proof": {"service_id": SERVICE, "timestamp": str(NOW), "signature": seal}}
    _refused("card_transaction_request_mismatch", body)


def test_a_response_for_another_transaction_or_phase_is_refused():
    _refused("card_transaction_id_mismatch", _signed(_record(transaction_id="b" * 64)))
    _refused("card_transaction_phase_mismatch", _signed(_record(phase="decision")))


def test_a_record_cannot_pick_another_card():
    _refused("card_transaction_card_mismatch", _signed(_record(access_id="another-card")))


def test_an_undecided_record_materializes_nothing():
    record = _record(phase="decision")
    _refused("card_transaction_undecided", _signed(record), phase="decision",
             expected_intent_digest=record["intent_digest"])


def test_a_moved_card_cannot_be_staged():
    _refused("card_transaction_revision_moved", _signed(_record()), current_revision=_authority().card_revision + 1)


def test_a_candidate_that_is_not_the_next_revision_is_refused():
    record = _record()
    record["candidate"] = {**record["candidate"], "card_revision": record["candidate_revision"] + 1}
    record["candidate_digest"] = change_digest(record["candidate"])
    record["intent_digest"] = ta.intent_digest(record)
    _refused("card_transaction_candidate_invalid", _signed(record))


@pytest.mark.parametrize("changes", [
    {"epoch": 0}, {"decision": "maybe"}, {"decision": "committed", "decided_at": 0},
    {"decided_at": NOW}, {"schema": "other.v1"},
])
def test_malformed_records_are_refused(changes):
    _refused("card_transaction_record_invalid", _signed(_record(**changes)))


def test_an_extra_or_missing_field_is_refused_before_anything_is_read():
    body = _signed(_record())
    body["asserted_user"] = "admin"
    _refused("card_transaction_record_invalid", body)


def test_a_short_secret_is_refused():
    with pytest.raises(ta.TransactionAuthorityRefused, match="authority_secret_invalid"):
        _signed(_record(), secret="short")


# ── Ops findings on b2101fa2 (11:38) ───────────────────────────────────────


def test_the_timestamp_is_inside_the_signature():
    body = _signed(_record())
    body["authority_proof"]["timestamp"] = str(NOW + 5)  # re-stamped after signing, still within skew
    _refused("authority_signature_invalid", body)


def test_a_decision_needs_the_local_receipts_intent():
    record = _record(phase="decision", decision="committed", decided_at=NOW)
    _refused("card_transaction_intent_required", _signed(record), phase="decision")
    _refused("card_transaction_intent_required", _signed(record), phase="decision", expected_intent_digest="x")


@pytest.mark.parametrize("echo", ["", "short", "g" * 32, "0" * 31, "A" * 32])
def test_a_malformed_request_echo_is_refused(echo):
    _refused("card_transaction_request_echo_invalid", _signed(_record()), request_echo=echo)


# Cross-implementation vectors (Ops finding 4): the authority's own JSON must
# reproduce these exactly. The intent digest uses ASCII-escaped canonical JSON
# (issuer_payload_digest); the candidate digest uses UTF-8 canonical JSON
# (change_digest); integers stay integers.
VECTOR_CANDIDATE_DIGEST = "7f2837f05883b29fa134eca0da7c2864b0e013153be3d78d3d4423c5853f4721"
VECTOR_INTENT_DIGEST = "58893fb2c11e1132b34d76afec55bd26748c0f7994a52b66fdc990e96f68d745"
VECTOR_SIGNATURE = "v1x9wKYlrRHcJ6cItFmeP87ID4CFVVGB3foXdncZOa0"
VECTOR_SECRET = "vector-secret-0123456789abcdef-0123"
VECTOR_TIMESTAMP = 1_790_000_000
VECTOR_ECHO = "00112233445566778899aabbccddeeff"


def _vector_record():
    candidate = {"access_id": "card-é", "card_revision": 8, "label": "Zürich — 東京", "ttl": 3600}
    record = {
        "schema": ta.RECORD_SCHEMA, "transaction_id": "a" * 64, "epoch": 3, "participant": "project",
        "binding_kind": "project", "binding_ref": "work:project:ñ", "actor_subject": "person-é",
        "actor_kind": "human", "action": "update", "subject_hash": "b" * 64, "access_id": "card-é",
        "expected_card_revision": 7, "candidate_revision": 8, "candidate_digest": change_digest(candidate),
        "expected_dependency_revisions": {"control-ü": 2}, "membership_incarnation": "m-1",
        "request_id": "r-1", "context_ref": "ctx-日本", "expires_at": 1_790_000_600,
        "audience": "connection-hub:tenant:project", "phase": "decision", "decision": "committed",
        "decided_at": 1_790_000_100, "request_echo": VECTOR_ECHO, "candidate": candidate,
    }
    record["intent_digest"] = ta.intent_digest(record)
    return record


def test_cross_implementation_vectors():
    record = _vector_record()
    body = ta.sign_transaction_authority(record, secret=VECTOR_SECRET, service_id="problem-board",
                                         now=VECTOR_TIMESTAMP)
    assert record["candidate_digest"] == VECTOR_CANDIDATE_DIGEST
    assert record["intent_digest"] == VECTOR_INTENT_DIGEST
    assert body["authority_proof"]["signature"] == VECTOR_SIGNATURE


def test_the_vectors_follow_from_the_written_algorithm_alone():
    """The algorithm an authority implements, in plain stdlib terms."""
    import base64
    import hashlib
    import hmac
    import json

    def canonical(value, *, ascii_only):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii_only, allow_nan=False)

    record = _vector_record()
    candidate_digest = hashlib.sha256(canonical(record["candidate"], ascii_only=False).encode("utf-8")).hexdigest()
    intent = {name: record[name] for name in ta.INTENT_FIELDS}
    intent_digest = hashlib.sha256(canonical(intent, ascii_only=True).encode("utf-8")).hexdigest()
    record_digest = hashlib.sha256(canonical(record, ascii_only=True).encode("utf-8")).hexdigest()
    message = "\n".join((ta.PROTOCOL, "problem-board", str(VECTOR_TIMESTAMP), VECTOR_ECHO, record_digest))
    signature = base64.urlsafe_b64encode(
        hmac.new(VECTOR_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert (candidate_digest, intent_digest, signature) == (
        VECTOR_CANDIDATE_DIGEST, VECTOR_INTENT_DIGEST, VECTOR_SIGNATURE)

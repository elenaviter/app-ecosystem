"""W502 v2: the Hub verifies a signed authority response over the ONE generic decision record."""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, IntentDraft

from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT, candidate_value_digest
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
    PROTOCOL,
    TransactionAuthorityRefused,
    card_authority_signature,
    verify_card_authority_v2,
)

TX = "t" * 8 + "0" * 56
SECRET = "s" * 40
SERVICE = "problem-board@1-0"
AUDIENCE = "connection-hub@1-0"
ECHO = "ab" * 16
NOW = 1_800_000_000
CANDIDATE = {"access_id": "aut_card", "original_revision": 3,
             "candidate": {"access_id": "aut_card", "card_revision": 4, "label": "edited"},
             "effects": [{"kind": "credential_lifetime", "key": "card",
                          "payload": {"access_id": "aut_card", "expires_at": 1_900_000_000, "base_card_revision": 3}}]}


def _hub_input(candidate=CANDIDATE):
    return {"participant": PARTICIPANT, "binding_kind": "connection-hub.card", "binding_ref": "aut_card",
            "target_scope": "s" * 64, "target_incarnation": 3, "action": "update", "before_revision": 3,
            "candidate_revision": 4, "candidate_digest": candidate_value_digest(candidate),
            "dependency_revisions": {}, "actor_subject": "user:admin", "actor_kind": "caller", "provisioning": {}}


def _record(state="preparing", decided_at=None, *, candidate=CANDIDATE, stored_candidate=None):
    draft = IntentDraft(replay_scope="pb:project", request_id="req-1", expires_at=NOW + 600,
                        participants=(PARTICIPANT,),
                        payload={"participant_inputs": {PARTICIPANT: _hub_input(candidate)},
                                 "participant_candidates": {PARTICIPANT: stored_candidate or candidate},
                                 "pb_state": {"project": "work:project:one"}})
    return DecisionRecord(draft.bind(TX, 7), state, {}, {}, decided_at=decided_at)


def _sign(record, *, phase="stage", echo=ECHO, timestamp=NOW, mutate=None):
    """A reference signer of the agreed framing (CodeApp 16:58); Apps' own is cross-checked below."""
    from service_foundation.coordination.durable_wire import participant_projection
    candidates = record.intent.as_mapping()["payload"]["participant_candidates"]
    unsigned = {
        "schema": PROTOCOL, "phase": phase, "request_echo": echo, "audience": AUDIENCE, "participant": PARTICIPANT,
        "global_intent_bytes": record.intent.canonical_bytes.decode("utf-8"),
        "global_intent_digest": record.intent.digest, "projection": participant_projection(record.intent, PARTICIPANT),
        "candidate": candidates[PARTICIPANT],
        "decision": "undecided" if phase == "stage" else record.state,
        "decided_at": None if phase == "stage" else record.decided_at,
    }
    if mutate:
        mutate(unsigned)
    return {**unsigned, "authority_proof": {
        "service_id": SERVICE, "timestamp": str(timestamp),
        "signature": card_authority_signature(unsigned, secret=SECRET, service_id=SERVICE, timestamp=str(timestamp))}}


def _verify(response, **overrides):
    kwargs = dict(secret=SECRET, service_id=SERVICE, audience=AUDIENCE, participant=PARTICIPANT,
                  transaction_id=TX, phase="stage", request_echo=ECHO, now=NOW)
    kwargs.update(overrides)
    return verify_card_authority_v2(response, **kwargs)


def test_a_stage_response_yields_the_persisted_intent_projection_and_candidate() -> None:
    record = _record()
    verified = _verify(_sign(record))
    assert verified.intent.digest == record.intent.digest and verified.intent.epoch == 7
    assert verified.projection["actor_subject"] == "user:admin"
    assert verified.candidate == CANDIDATE and (verified.decision, verified.decided_at) == ("undecided", None)


@pytest.mark.parametrize("state", ["committed", "aborted"])
def test_a_decision_response_yields_the_recorded_terminal_value(state) -> None:
    record = _record(state, decided_at=NOW - 5)
    verified = _verify(_sign(record, phase="decision"), phase="decision")
    assert (verified.decision, verified.decided_at) == (state, NOW - 5)


@pytest.mark.parametrize(("override", "reason"), [
    ({"request_echo": "cd" * 16}, "authority_response_mismatch"),
    ({"audience": "other-hub"}, "authority_response_mismatch"),
    ({"participant": "problem-board.policy"}, "authority_response_mismatch"),
    ({"phase": "decision"}, "authority_response_mismatch"),
    ({"service_id": "other-service"}, "authority_service_mismatch"),
    ({"transaction_id": "u" * 64}, "authority_transaction_mismatch"),
    ({"secret": "x" * 40}, "authority_signature_invalid"),
    ({"secret": "short"}, "authority_secret_invalid"),
    ({"now": NOW + 301}, "authority_proof_stale"),
    ({"request_echo": "not-hex"}, "authority_request_invalid"),
])
def test_a_response_not_bound_to_this_request_is_refused(override, reason) -> None:
    with pytest.raises(TransactionAuthorityRefused, match=reason):
        _verify(_sign(_record()), **override)


def _tamper(field, value):
    def mutate(unsigned):
        unsigned[field] = value(unsigned) if callable(value) else value
    return mutate


@pytest.mark.parametrize(("mutate", "reason"), [
    (_tamper("global_intent_digest", "f" * 64), "authority_intent_digest_mismatch"),
    (_tamper("global_intent_bytes", lambda u: u["global_intent_bytes"].replace(":", ": ", 1)), "authority_intent_invalid"),
    (_tamper("projection", lambda u: {**u["projection"], "candidate_revision": 5}), "authority_projection_mismatch"),
    (_tamper("projection", lambda u: {**u["projection"], "actor_subject": "user:forged"}), "authority_projection_mismatch"),
    (_tamper("candidate", lambda u: {**u["candidate"], "effects": []}), "authority_candidate_mismatch"),
    (_tamper("candidate", lambda u: {**u["candidate"], "original_revision": 2}), "authority_candidate_invalid"),
    (_tamper("candidate", lambda u: {**u["candidate"], "access_id": "aut_other"}), "authority_candidate_invalid"),
    (_tamper("candidate", lambda u: {**u["candidate"], "extra": 1}), "authority_candidate_invalid"),
    (_tamper("decision", "committed"), "authority_decision_invalid"),
    (_tamper("decided_at", 5), "authority_decision_invalid"),
])
def test_a_signed_but_inconsistent_response_is_refused(mutate, reason) -> None:
    # Signed AFTER tampering: the proof is valid, so only the content checks can refuse.
    with pytest.raises(TransactionAuthorityRefused, match=reason):
        _verify(_sign(_record(), mutate=mutate))


def test_a_changed_field_after_signing_breaks_the_proof() -> None:
    response = _sign(_record())
    response = {**response, "candidate": {**response["candidate"], "effects": []}}
    with pytest.raises(TransactionAuthorityRefused, match="authority_signature_invalid"):
        _verify(response)


@pytest.mark.parametrize("shape", ["extra_key", "missing_key", "proof_extra"])
def test_the_exact_key_sets_are_required(shape) -> None:
    response = copy.deepcopy(_sign(_record()))
    if shape == "extra_key":
        response["note"] = "x"
    elif shape == "missing_key":
        del response["decided_at"]
    else:
        response["authority_proof"]["kid"] = "k1"
    with pytest.raises(TransactionAuthorityRefused):
        _verify(response)


def test_a_stage_after_the_intent_expired_is_refused() -> None:
    record = _record()
    with pytest.raises(TransactionAuthorityRefused, match="authority_intent_expired"):
        _verify(_sign(record, timestamp=NOW + 600), now=NOW + 600)


@pytest.mark.parametrize("decided", [("undecided", None), ("committed", None), ("committed", -1), ("pending", 3)])
def test_a_decision_phase_needs_a_recorded_terminal_value(decided) -> None:
    record = _record("committed", decided_at=NOW)
    mutate = _tamper("decision", decided[0])

    def both(unsigned):
        mutate(unsigned)
        unsigned["decided_at"] = decided[1]

    with pytest.raises(TransactionAuthorityRefused, match="authority_decision_invalid"):
        _verify(_sign(record, phase="decision", mutate=both), phase="decision")


def _apps_signer():
    path = os.environ.get("APPS_CARD_BUSINESS_AUTHORITY")
    if not path:
        pytest.skip("APPS_CARD_BUSINESS_AUTHORITY (Apps services/card_business_authority.py) is not set")
    spec = importlib.util.spec_from_file_location("apps_card_business_authority", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("phase", "state"), [("stage", "prepared"), ("decision", "committed"), ("decision", "aborted")])
def test_cross_check_the_apps_signer_verifies_here(phase, state) -> None:
    apps = _apps_signer()
    record = _record(state, decided_at=None if phase == "stage" else NOW - 1)
    response = apps.build_authority_response(record, phase=phase, request_echo=ECHO, audience=AUDIENCE,
                                             participant=PARTICIPANT, service_id=SERVICE, secret=SECRET, now=NOW)
    assert response == _sign(record, phase=phase)  # byte-for-byte the same agreed framing
    verified = _verify(response, phase=phase)
    assert verified.candidate == CANDIDATE


def test_cross_check_the_apps_signer_refuses_what_we_refuse() -> None:
    apps = _apps_signer()
    record = _record(stored_candidate={**CANDIDATE, "effects": []})  # stored value no longer hashes to the digest
    with pytest.raises(Exception, match="authority_candidate_mismatch"):
        apps.build_authority_response(record, phase="stage", request_echo=ECHO, audience=AUDIENCE,
                                      participant=PARTICIPANT, service_id=SERVICE, secret=SECRET, now=NOW)
    with pytest.raises(TransactionAuthorityRefused, match="authority_candidate_mismatch"):
        _verify(_sign(record))


def test_a_candidate_for_another_card_than_the_projection_binds_is_refused() -> None:
    # Self-consistent candidate (its digest is in the projection) but for another Card.
    other = {**CANDIDATE, "access_id": "aut_other", "candidate": {**CANDIDATE["candidate"], "access_id": "aut_other"}}
    with pytest.raises(TransactionAuthorityRefused, match="authority_candidate_invalid"):
        _verify(_sign(_record(candidate=other)))

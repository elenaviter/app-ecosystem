"""Frozen cross-application authority bytes and reject vectors."""

from __future__ import annotations

import pytest

from service_foundation.coordination.durable_wire import (
    GlobalIntent, IntentDraft, WireRefused, canonical_json_bytes, parse_canonical_json_bytes,
    participant_projection, projection_digest,
)


def selected_input():
    return {
        "participant": "card", "binding_kind": "card", "binding_ref": "card-a",
        "target_scope": "project-a", "target_incarnation": "inc-a",
        "action": "update", "before_revision": 2, "candidate_revision": 3,
        "candidate_digest": "a" * 64, "dependency_revisions": {"member-a": 1},
        "actor_subject": "user-a", "actor_kind": "human", "provisioning": None,
    }


def vector():
    return GlobalIntent("tx-a", 7, "request-a", 1800000000, ("card",),
                        {"participant_inputs": {"card": selected_input()},
                         "project_ref": "project-a"})


def test_frozen_global_intent_and_projection_vectors():
    intent = vector()
    assert intent.digest == "1192d0b514933591e4b1c35e5de427697f4e781ab4d525e52138af8d8b5b6d51"
    assert projection_digest(intent, "card") == (
        "436fef32e30b23c40ea6eae2329a6d4843ed00b59f409290cb50e2b46d62c663")
    assert GlobalIntent.from_canonical_bytes(intent.canonical_bytes).digest == intent.digest
    assert participant_projection(intent, "card")["global_intent_digest"] == intent.digest
    assert b'"expires_at":1800000000' in intent.canonical_bytes
    assert b'"schema":"durable-transaction-intent.v1"' in intent.canonical_bytes


def test_projection_is_selected_from_original_intent_payload():
    intent = vector()
    other = selected_input()
    other["candidate_digest"] = "b" * 64
    self_consistent_later_projection = {
        "global_intent_digest": intent.digest, **other,
    }
    assert self_consistent_later_projection != participant_projection(intent, "card")
    assert canonical_json_bytes(self_consistent_later_projection) != canonical_json_bytes(
        participant_projection(intent, "card"))


@pytest.mark.parametrize("value", [1.5, float("nan"), -0.0, {"a": 1.0},
                                    {1: "bad"}, "\ud800"])
def test_noncanonical_values_are_rejected(value):
    with pytest.raises(WireRefused):
        canonical_json_bytes(value)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"a": 1}',
                                   b'{"x":-0}', b'{"x":1.2}', b'{"x":NaN}',
                                   b'"\\ud800"'])
def test_noncanonical_input_bytes_are_rejected(data):
    with pytest.raises(WireRefused):
        parse_canonical_json_bytes(data)


def test_integer_coordinates_reject_bool_and_floating_point():
    for epoch, expires in [(True, 1800000000), (7, False), (1.0, 1800000000)]:
        with pytest.raises(WireRefused, match="intent_invalid"):
            GlobalIntent("tx-a", epoch, "request-a", expires, ("card",), {})


def test_global_intent_bytes_freeze_caller_payload():
    payload = {"participant_inputs": {"card": selected_input()}}
    intent = GlobalIntent("tx-a", 7, "request-a", 1800000000, ("card",), payload)
    digest = intent.digest
    payload["participant_inputs"]["card"]["candidate_digest"] = "b" * 64
    assert intent.digest == digest
    assert participant_projection(intent, "card")["candidate_digest"] == "a" * 64


def test_draft_freezes_payload_and_binds_coordinates_before_hash():
    payload = {"participant_inputs": {"card": selected_input()}}
    draft = IntentDraft("project-a:user-a", "request-a", 1800000000,
                        ("card",), payload)
    payload["participant_inputs"]["card"]["candidate_digest"] = "b" * 64
    first = draft.bind("tx-a", 7)
    second = draft.bind("tx-b", 8)
    assert first.digest != second.digest
    assert participant_projection(first, "card")["candidate_digest"] == "a" * 64
    assert "replay_scope" not in first.as_mapping()

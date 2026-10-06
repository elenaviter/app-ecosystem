"""W578: the shared connection-hub.card-group vectors, pinned on the Hub side.

``fixtures/w578_hub_card_group_vectors.json`` is the group input the Hub
stages for several Cards under one decision (CodeApp 23:01 agreed the
semantics and bounds); Problem Board consumes the same file. Each vector is
complete (a candidate value, its reads and catalog, and the participant
input), never a patch, and each refused vector names the Hub's reason.

The C1 one-Card vectors (``w502_hub_participant_vectors.json``) are unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from connection_hub.delegated_credentials.cards.card_group import (
    GROUP_BINDING_KIND, MAX_GROUP_MEMBERS, group_candidate_value, group_member, hub_group_participant_input,
    validate_group_candidate, verify_group_projection,
)
from connection_hub.delegated_credentials.cards.card_participant import reads_from_dependencies
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import subject_hash_for

FIXTURE = Path(__file__).parent / "fixtures" / "w578_hub_card_group_vectors.json"
C1_FIXTURE = Path(__file__).parent / "fixtures" / "w502_hub_participant_vectors.json"

PROJECT = "work:project:quickstart"
PERSON = "platform-user-2"
PROJECT_SUBJECT = "project-authority:a53c2d60520c56cde0f2e91bdd6327564eaf3b508b342d3500926e364bc237bd"
CREATOR = "project-creator"
P_ID = "control-624192ef1bc6c4d30ce85bed"
C_ID = "person-control-ab5f7fb1f5ecc82017c40de6"
MY_ID = "person-my-card-ab5f7fb1f5ecc82017c40de6"
PENDING_ID = "invitation-control-7f3a"
ACTOR = "platform-user-2"
CATALOG = "c" * 64
NOW = 1_800_000_000


def _card(**fields) -> CardAuthority:
    base = json.loads(C1_FIXTURE.read_text())["accepted"]["candidate_value"]["candidate"]
    # Credentialless Control Cards: no expiry, no credential bookkeeping.
    return CardAuthority.from_mapping({**base, "provenance": {}, "expires_at": 0, **fields})


def _binding(control_id, holder="", issuer_kind="application", issuer_ref=PROJECT):
    return {"control_id": control_id, "issuer_ref": issuer_ref, "issuer_kind": issuer_kind, "issuer_label": "",
            "manage_url": "", "control_revision": 1, "holder_subject": holder}


def _members():
    control = _card(access_id=C_ID, grantor_subject=PROJECT_SUBJECT, client_id="control-card:project",
                    delegate_subject="", source="control", card_kind="control", label="Invited member",
                    card_revision=1, issuer_ref=PROJECT, issuer_kind="project_person_control",
                    control_card=_binding(P_ID, holder=CREATOR))
    my_card = _card(access_id=MY_ID, grantor_subject=PERSON, client_id="project-person:my-card",
                    delegate_subject="", source="control", card_kind="control", label="My card",
                    card_revision=1, issuer_ref=PROJECT, issuer_kind="project_person_my_card",
                    control_card=_binding(C_ID, holder=PROJECT_SUBJECT, issuer_kind="project_person_control"))
    pending_before = _card(access_id=PENDING_ID, grantor_subject=PROJECT_SUBJECT, client_id="control-card:project",
                           delegate_subject="", source="control", card_kind="control", label="Invitation",
                           card_revision=1, issuer_ref=PROJECT, issuer_kind="project_invitation_control")
    pending_after = CardAuthority.from_mapping({**pending_before.to_dict(), "card_revision": 2, "state": "revoked"})
    return [group_member(original=None, candidate=control, action="create"),
            group_member(original=None, candidate=my_card, action="create"),
            group_member(original=pending_before, candidate=pending_after, action="revoke")]


P_READ = {"subject_hash": subject_hash_for(CREATOR), "access_id": P_ID, "revision": 1}


def _vector(name, members, *, reads=(P_READ,), effects=(), catalog=CATALOG, actor_kind="grantor"):
    value = group_candidate_value(members, effects)
    return {"name": name, "candidate_value": value, "candidate_digest": sha256_hex(canonical_json_bytes(value)),
            "reads": [dict(read) for read in reads], "catalog_digest": catalog,
            "participant_input": hub_group_participant_input(
                members=members, actor_subject=ACTOR, actor_kind=actor_kind, effects=effects, reads=reads,
                catalog_version_digest=catalog)}


def _refused(name, reason, value, *, reads=(P_READ,)):
    """A complete candidate value the Hub refuses with ``reason`` (no input is built from it)."""
    return {"name": name, "reason": reason, "candidate_value": value, "reads": [dict(read) for read in reads]}


def build_vectors() -> dict:
    members = _members()
    accepted = _vector("invitation_redemption_group", members)
    good = accepted["candidate_value"]

    def changed(mutate):
        value = copy.deepcopy(good)
        mutate(value)
        return value

    def member(value, access_id):
        return next(entry for entry in value["cards"] if entry["access_id"] == access_id)

    def absent_at_two(value):
        member(value, C_ID)["candidate"]["card_revision"] = 2

    def present_as_create(value):
        member(value, PENDING_ID)["action"] = "create"

    def wrong_scope(value):
        member(value, MY_ID)["subject_hash"] = "0" * 64

    unsorted = changed(lambda value: value["cards"].reverse())
    duplicate = changed(lambda value: value["cards"].append(copy.deepcopy(value["cards"][0])))
    too_many = changed(lambda value: value.__setitem__("cards", value["cards"] * (MAX_GROUP_MEMBERS // 3 + 1)))
    refused = [
        _refused("empty_group", "card_group_empty", changed(lambda value: value.__setitem__("cards", []))),
        _refused("too_many_members", "card_group_too_large", too_many),
        _refused("duplicate_target", "card_group_member_duplicate", duplicate),
        _refused("members_not_canonical", "card_group_not_canonical", unsorted),
        _refused("absent_original_not_revision_one", "card_group_absent_original_invalid", changed(absent_at_two)),
        _refused("existing_original_as_create", "card_group_member_revision_invalid", changed(present_as_create)),
        _refused("member_scope_not_its_grantor", "card_group_member_invalid", changed(wrong_scope)),
        _refused("read_overlaps_target", "card_group_read_overlaps_target", good,
                 reads=(P_READ, {"subject_hash": subject_hash_for(PERSON), "access_id": MY_ID, "revision": 0})),
        _refused("present_and_absent_read", "card_group_dependency_contradiction", good,
                 reads=(P_READ, {**P_READ, "revision": 0})),
        _refused("project_control_read_missing", "card_group_control_read_missing", good, reads=()),
        _refused("project_control_read_absent", "card_group_control_read_missing", good,
                 reads=({**P_READ, "revision": 0},)),
        _refused("read_revision_not_an_integer", "card_group_read_invalid", good,
                 reads=({**P_READ, "revision": "1"},)),
        _refused("read_revision_negative", "card_group_read_invalid", good, reads=({**P_READ, "revision": -1},)),
        _refused("read_extra_field", "card_group_read_invalid", good, reads=({**P_READ, "note": "x"},)),
    ]
    return {
        "schema": "w578.hub-card-group-vectors.v1",
        "about": ("The connection-hub.card-group participant input (CodeApp 23:01). participant_inputs["
                  "'connection-hub.card'] = participant_input and participant_candidates['connection-hub.card'] = "
                  "candidate_value; candidate_digest = sha256(kernel canonical_json_bytes(candidate_value)). The "
                  "aggregate's own revision fields are fixed (before 0, candidate 1, incarnation 1, action create); "
                  "binding_ref names the members at their base revisions and is NOT unique per transaction (a replay "
                  "after ABORT reuses it): the transaction id, epoch and global intent digest separate transactions. "
                  "A bound parent Control must be a member or a PRESENT read (revision >= 1). "
                  "Members are sorted by (subject_hash, access_id); an "
                  "absent original is explicit (original_absent true, original_revision 0, candidate revision 1, "
                  "action create). Each refused vector is complete and names the Hub's refusal."),
        "binding_kind": GROUP_BINDING_KIND,
        "accepted": accepted,
        "refused": refused,
    }


VECTORS = json.loads(FIXTURE.read_text())


def test_the_hub_builder_reproduces_the_shared_group_vectors_exactly():
    assert build_vectors() == VECTORS


def test_the_accepted_group_verifies_against_its_own_projection():
    accepted = VECTORS["accepted"]
    projection = {"global_intent_digest": "f" * 64, **accepted["participant_input"]}
    checked = verify_group_projection(projection, accepted["candidate_value"])
    assert checked == accepted["candidate_value"]
    assert reads_from_dependencies(accepted["participant_input"]["dependency_revisions"]) == sorted(
        accepted["reads"], key=lambda read: (read["subject_hash"], read["access_id"]))
    assert accepted["participant_input"]["candidate_digest"] == accepted["candidate_digest"]


def test_the_kernel_accepts_the_group_projection_and_binds_its_digest():
    from service_foundation.coordination.durable_decision_log import IntentDraft
    from service_foundation.coordination.durable_wire import participant_projection

    from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT

    accepted = VECTORS["accepted"]
    draft = IntentDraft(replay_scope="pb:group-vectors", request_id="pb-group-vector", expires_at=NOW + 600,
                        participants=(PARTICIPANT,),
                        payload={"participant_inputs": {PARTICIPANT: accepted["participant_input"]},
                                 "participant_candidates": {PARTICIPANT: accepted["candidate_value"]}})
    intent = draft.bind("a" * 64, 1)
    projection = participant_projection(intent, PARTICIPANT)
    assert projection["global_intent_digest"] == intent.digest
    assert verify_group_projection(projection, accepted["candidate_value"]) == accepted["candidate_value"]


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=[v["name"] for v in VECTORS["refused"]])
def test_each_refused_group_is_refused_by_name(vector):
    with pytest.raises(DecisionRefused, match=f"^{vector['reason']}$"):
        validate_group_candidate(vector["candidate_value"], reads=vector["reads"])


@pytest.mark.parametrize("field,value", [
    ("binding_ref", "group:" + "0" * 64), ("target_scope", "0" * 64), ("target_incarnation", 2),
    ("action", "update"), ("before_revision", 1), ("candidate_revision", 2), ("candidate_digest", "0" * 64),
    ("provisioning", {"x": 1}), ("actor_kind", "human"), ("actor_subject", ""), ("actor_subject", 7),
    ("actor_subject", " platform-user-2"), ("actor_subject", None),
])
def test_any_changed_aggregate_field_is_not_bound(field, value):
    accepted = VECTORS["accepted"]
    projection = {**accepted["participant_input"], field: value}
    with pytest.raises(DecisionRefused, match="^card_group_not_bound$"):
        verify_group_projection(projection, accepted["candidate_value"])


@pytest.mark.parametrize("actor", ["", " padded", 7, None])
def test_the_builder_refuses_a_malformed_actor(actor):
    with pytest.raises(DecisionRefused, match="^card_group_actor_invalid$"):
        hub_group_participant_input(members=_members(), actor_subject=actor, actor_kind="grantor", reads=(P_READ,))


def test_a_changed_member_changes_the_digest_and_binding():
    accepted = VECTORS["accepted"]
    value = copy.deepcopy(accepted["candidate_value"])
    next(entry for entry in value["cards"] if entry["access_id"] == PENDING_ID)["candidate"]["label"] = "other"
    with pytest.raises(DecisionRefused, match="^card_group_not_bound$"):
        verify_group_projection(accepted["participant_input"], value)


def test_the_one_card_c1_vectors_are_unchanged():
    # Pinned digest of the C1 file as it stood at main 6d07de35.
    assert sha256_hex(C1_FIXTURE.read_bytes()) == C1_DIGEST


C1_DIGEST = "cf0d25fa03c02414a62a3d8ed66c2532571bc5da49d9b9c43cbf5946a6e5d8d7"

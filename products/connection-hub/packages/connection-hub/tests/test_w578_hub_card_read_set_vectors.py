"""W578: the shared connection-hub.card-read-set vectors, pinned on the Hub side.

``fixtures/w578_hub_card_read_set_vectors.json`` is the input for a
transaction that changes no Card but holds Cards, absences and the active
catalog (CodeApp 23:19, confirmed 23:27). The C1 and card-group vector files
are unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused, IntentDraft
from service_foundation.coordination.durable_wire import canonical_json_bytes, participant_projection, sha256_hex

from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.card_read_set import (
    MAX_READ_SET_READS, READ_SET_BINDING_KIND, hub_read_set_participant_input, read_set_candidate_value,
    validate_read_set_candidate, verify_read_set_projection,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for

FIXTURE = Path(__file__).parent / "fixtures" / "w578_hub_card_read_set_vectors.json"
PINNED = {"w502_hub_participant_vectors.json": None, "w578_hub_card_group_vectors.json":
          "9b82058d9e90ac4a445624878d3b6dac36cfee26af25902f02c68de9b607237e"}
ADMIN, TARGET = "platform-user-1", "platform-user-2"
CATALOG = "a" * 64
NOW = 1_800_000_000
READS = [
    {"subject_hash": subject_hash_for(ADMIN), "access_id": "person-my-card-admin", "revision": 4},
    {"subject_hash": subject_hash_for(TARGET), "access_id": "person-my-card-target", "revision": 0},
    {"subject_hash": subject_hash_for("project-creator"), "access_id": "control-624192ef1bc6c4d30ce85bed",
     "revision": 2},
]


def _refused(name, reason, value):
    return {"name": name, "reason": reason, "candidate_value": value}


def build_vectors() -> dict:
    participant_input = hub_read_set_participant_input(reads=READS, catalog_version_digest=CATALOG,
                                                       actor_subject=ADMIN, actor_kind="caller")
    value = read_set_candidate_value(READS, CATALOG)
    good = validate_read_set_candidate(value)

    def changed(mutate):
        result = copy.deepcopy(good)
        mutate(result)
        return result

    many = [{"subject_hash": "0" * 63 + format(i % 16, "x"), "access_id": f"card-{i:03d}", "revision": 1}
            for i in range(MAX_READ_SET_READS + 1)]
    refused = [
        _refused("empty_without_catalog", "card_read_set_empty",
                 changed(lambda v: (v.__setitem__("reads", []), v.__setitem__("catalog", "")))),
        _refused("too_many_reads", "card_read_set_too_large",
                 changed(lambda v: v.__setitem__("reads", sorted(many, key=lambda r: (r["subject_hash"],
                                                                                      r["access_id"]))))),
        _refused("reads_not_canonical", "card_read_set_not_canonical", changed(lambda v: v["reads"].reverse())),
        _refused("same_card_twice", "card_read_set_dependency_contradiction",
                 changed(lambda v: v["reads"].insert(0, dict(v["reads"][0])))),
        _refused("present_and_absent_together", "card_read_set_dependency_contradiction",
                 changed(lambda v: v["reads"].insert(0, {**v["reads"][0], "revision": 0}))),
        _refused("negative_revision", "card_read_set_read_invalid",
                 changed(lambda v: v["reads"][0].__setitem__("revision", -1))),
        _refused("revision_not_an_integer", "card_read_set_read_invalid",
                 changed(lambda v: v["reads"][0].__setitem__("revision", "4"))),
        _refused("scope_not_hex", "card_read_set_read_invalid",
                 changed(lambda v: v["reads"][0].__setitem__("subject_hash", "platform-user-1"))),
        _refused("extra_read_field", "card_read_set_read_invalid",
                 changed(lambda v: v["reads"][0].__setitem__("note", "x"))),
        _refused("catalog_not_hex", "card_read_set_invalid", changed(lambda v: v.__setitem__("catalog", "v1"))),
        _refused("wrong_schema", "card_read_set_invalid", changed(lambda v: v.__setitem__("schema", "other"))),
    ]
    return {
        "schema": "w578.hub-card-read-set-vectors.v1",
        "about": ("The connection-hub.card-read-set participant input (CodeApp 23:19, confirmed 23:27): a transaction "
                  "that changes no Card but holds Cards (revision >= 1), absences (revision 0) and the active "
                  "catalog until its one decision. participant_inputs['connection-hub.card'] = participant_input, "
                  "participant_candidates['connection-hub.card'] = candidate_value; candidate_digest = sha256(kernel "
                  "canonical_json_bytes(candidate_value)); binding_ref = 'reads:' + that digest; action read, "
                  "before_revision 1, candidate_revision 1, incarnation 1; dependency_revisions are EXACTLY the "
                  "candidate's reads and catalog. An empty catalog is allowed by the wire, never a licence to drop "
                  "a catalog dependency a caller's check needs. Each refused vector is complete and names the "
                  "Hub's refusal."),
        "binding_kind": READ_SET_BINDING_KIND,
        "accepted": {"name": "admin_change_holding_actor_target_and_project_control", "candidate_value": value,
                     "candidate_digest": sha256_hex(canonical_json_bytes(value)),
                     "participant_input": participant_input},
        "refused": refused,
    }


VECTORS = json.loads(FIXTURE.read_text())


def test_the_hub_builder_reproduces_the_shared_read_set_vectors_exactly():
    assert build_vectors() == VECTORS


def test_the_kernel_accepts_the_read_set_and_the_hub_verifies_it():
    accepted = VECTORS["accepted"]
    draft = IntentDraft(replay_scope="pb:read-set", request_id="pb-read-set", expires_at=NOW + 600,
                        participants=(PARTICIPANT,),
                        payload={"participant_inputs": {PARTICIPANT: accepted["participant_input"]},
                                 "participant_candidates": {PARTICIPANT: accepted["candidate_value"]}})
    projection = participant_projection(draft.bind("a" * 64, 1), PARTICIPANT)
    assert verify_read_set_projection(projection, accepted["candidate_value"]) == accepted["candidate_value"]
    assert accepted["participant_input"]["candidate_digest"] == accepted["candidate_digest"]


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=[v["name"] for v in VECTORS["refused"]])
def test_each_refused_read_set_is_refused_by_name(vector):
    with pytest.raises(DecisionRefused, match=f"^{vector['reason']}$"):
        validate_read_set_candidate(vector["candidate_value"])


@pytest.mark.parametrize("field,value", [
    ("binding_ref", "reads:" + "0" * 64), ("target_scope", "0" * 64), ("target_incarnation", 2),
    ("target_incarnation", True), ("before_revision", True), ("candidate_revision", True),
    ("action", "update"), ("before_revision", 0), ("candidate_revision", 2), ("candidate_digest", "0" * 64),
    ("provisioning", {"x": 1}), ("actor_kind", "human"), ("actor_subject", ""), ("actor_subject", 7),
    ("dependency_revisions", {}),
])
def test_any_changed_aggregate_field_is_not_bound(field, value):
    accepted = VECTORS["accepted"]
    with pytest.raises(DecisionRefused, match="^card_read_set_not_bound$"):
        verify_read_set_projection({**accepted["participant_input"], field: value}, accepted["candidate_value"])


def test_a_dropped_dependency_is_not_bound():
    accepted = VECTORS["accepted"]
    dependencies = dict(accepted["participant_input"]["dependency_revisions"])
    dependencies.pop(next(key for key in dependencies if key.startswith("catalog-active:")))
    with pytest.raises(DecisionRefused, match="^card_read_set_not_bound$"):
        verify_read_set_projection({**accepted["participant_input"], "dependency_revisions": dependencies},
                                   accepted["candidate_value"])


def test_the_c1_and_group_vectors_are_unchanged():
    for name, digest in PINNED.items():
        current = sha256_hex((Path(__file__).parent / "fixtures" / name).read_bytes())
        if digest is not None:
            assert current == digest
        else:
            from test_w578_hub_card_group_vectors import C1_DIGEST
            assert current == C1_DIGEST

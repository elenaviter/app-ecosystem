"""Owner-only My settings: closed wire, immutable STAGE audit and exact display."""
from __future__ import annotations

import copy
import dataclasses
from datetime import datetime

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import canonical_json_bytes

from connection_hub.delegated_credentials.cards.card_group import group_candidate_value
from connection_hub.delegated_credentials.cards.model import CardAuthority, ControlCardBinding
from connection_hub.delegated_credentials.cards.participant_card_version import stage_authorization
from connection_hub.delegated_credentials.controls.project_person import PROJECT_PERSON_CONTROL_ISSUER_KIND
from connection_hub.delegated_credentials.managed_card_edit_forward import ManagedCardEditError, managed_card_edit_body
from connection_hub.delegated_credentials.my_card_person_properties_plan import (
    SETTINGS_STEP, build_person_settings_update, settings_patch,
)
from connection_hub.delegated_credentials.plan_display import plan_display
from connection_hub.delegated_credentials.project_authorization import ProjectAuthorizationDecision
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    MY_CARD_COMMIT_EMAIL_PROPERTY as EMAIL, MY_CARD_GITHUB_PROPERTY as GITHUB,
    PROJECT_IDENTITY_EDGE_PROVENANCE, PROJECT_PERSON_MY_CARD_ISSUER_KIND,
    PROJECT_PERSON_SELECTION_SOURCE, ProjectPersonCardIdentity,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for

PROJECT, ACTOR, DIGEST = "work:project:synthetic-settings", "synthetic-owner", "d" * 64
AT = "2026-10-11T12:00:01.125000+00:00"
GH = {"provider_id": "github", "account_id": "synthetic-account", "login": "synthetic-login"}


def original():
    identity = ProjectPersonCardIdentity.build(project_ref=PROJECT, person_subject=ACTOR)
    return CardAuthority(
        access_id=identity.my_card_id, client_id=identity.client_id, grantor_subject=ACTOR,
        delegate_subject=ACTOR, source=PROJECT_PERSON_SELECTION_SOURCE, card_kind="automation",
        card_revision=3, catalog_version="synthetic-v1", issuer_ref=PROJECT,
        issuer_kind=PROJECT_PERSON_MY_CARD_ISSUER_KIND, label="My synthetic Card",
        provenance={PROJECT_IDENTITY_EDGE_PROVENANCE: identity.marker(), "kept": {"a": 1}},
        properties={"kept": {"value": True}, EMAIL: {"email": "old@example.test", "set_by": ACTOR, "set_at": 1}},
        resource_grants={"synthetic": ("read",)}, resource_operations={"synthetic": ("get",)},
        control_card=ControlCardBinding(identity.control_id, PROJECT, PROJECT_PERSON_CONTROL_ISSUER_KIND,
                                        holder_subject=identity.project_subject))


def stage(card, patch):
    return {"scope": PROJECT, "request_id": "synthetic-settings-1", "actor_subject": ACTOR,
        "delegable_grants": ["read"], "project_control": {"control_id": "synthetic-project-control",
        "holder_subject": ACTOR}, "creations": [], "at": AT, "updates": [{
        "kind": SETTINGS_STEP, "target_subject": ACTOR, "access_id": card.access_id,
        "subject_hash": subject_hash_for(ACTOR), "original_revision": card.card_revision,
        "selection": {"properties": patch}}]}


def build(card, patch, *, decision=None, actor=ACTOR, digest=DIGEST):
    decision = decision or stage_authorization(stage(card, patch), DIGEST).decision_for("update:0")
    return build_person_settings_update(original=card, selection={"properties": patch}, decision=decision,
        project_ref=PROJECT, target_subject=ACTOR, actor_subject=actor,
        request_id="synthetic-settings-1", request_digest=digest)["member"]


@pytest.mark.parametrize("patch", [
    {}, {"unknown": None}, {GITHUB: "account"}, {GITHUB: {**GH, "token": "synthetic-forbidden"}},
    {GITHUB: {**GH, "linked_at": 1}}, {GITHUB: {"provider_id": "github", "account_id": "a"}},
    {EMAIL: {"email": "invalid"}}, {EMAIL: {"email": "e@example.test", "set_by": ACTOR}},
    {EMAIL: {"email": "e@example.test", "credential": "synthetic-forbidden"}},
])
def test_wire_patch_is_closed_and_never_accepts_audit_or_credential_fields(patch):
    with pytest.raises(ValueError, match="project_person_my_card_property_invalid"):
        settings_patch(patch)


def test_internal_audits_are_discarded_before_signing():
    assert settings_patch({GITHUB: {**GH, "linked_at": 1}, EMAIL: {
        "email": "e@example.test", "set_by": "synthetic-admin", "set_at": 2}}, internal_audit=True) == {
        GITHUB: GH, EMAIL: {"email": "e@example.test"}}


@pytest.mark.parametrize("extra", [dict(label="new"), dict(composition_mode="or"),
    dict(accepted_operations={}), dict(expected_catalog_version="v1")])
def test_settings_never_mix_with_other_edits(extra):
    with pytest.raises(ManagedCardEditError):
        managed_card_edit_body(actor_subject=ACTOR, project_ref=PROJECT, request_id="r-1", kind="person_my",
            principal_key="user:" + ACTOR, original_revision=3, selection={"properties": {GITHUB: GH}}, **extra)


def test_stage_adds_evidence_only_to_the_settings_step_without_mutating_input():
    data = stage(original(), {GITHUB: GH})
    data["updates"].append({"kind": "reselect", "target_subject": ACTOR, "access_id": "synthetic-other",
                            "subject_hash": "a" * 64, "original_revision": 1, "selection": {}})
    before = canonical_json_bytes(data)
    auth = stage_authorization(data, DIGEST)
    assert canonical_json_bytes(data) == before
    assert dict(auth.decision_for("update:0").evidence) == {
        "source": "card-version-stage", "request_digest": DIGEST, "person_settings_at": AT}
    plain = auth.decision_for("update:1")
    assert plain == ProjectAuthorizationDecision.allow(auth.request.step_request(auth.request.steps[1]),
        delegable_grants=data["delegable_grants"], project_control=plain.project_control)


def test_builder_changes_only_the_two_settings_and_revision_using_stage_time():
    card = original()
    member = build(card, {GITHUB: GH, EMAIL: None})
    candidate = member["candidate"]
    assert candidate["properties"] == {"kept": {"value": True}, GITHUB: {
        **GH, "linked_at": int(datetime.fromisoformat(AT).timestamp())}}
    restored = copy.deepcopy(candidate)
    restored["properties"], restored["card_revision"] = card.to_dict()["properties"], card.card_revision
    assert restored == card.to_dict()
    assert member["original_revision"] == card.card_revision


@pytest.mark.parametrize("case", ["foreign_actor", "missing_stage", "wrong_digest", "bad_time", "wrong_decision_digest"])
def test_unbound_or_foreign_settings_refuse_without_mutating_original(case):
    card = original()
    before = canonical_json_bytes(card.to_dict())
    decision = stage_authorization(stage(card, {GITHUB: GH}), DIGEST).decision_for("update:0")
    actor, digest = ACTOR, DIGEST
    if case == "foreign_actor":
        actor = "synthetic-admin"
    elif case == "missing_stage":
        decision = dataclasses.replace(decision, evidence={})
    elif case == "wrong_digest":
        digest = "e" * 64
    elif case == "wrong_decision_digest":
        decision = dataclasses.replace(decision, request_digest="e" * 64)
    else:
        decision = dataclasses.replace(decision, evidence={**decision.evidence, "person_settings_at": "invalid"})
    with pytest.raises(DecisionRefused):
        build(card, {GITHUB: GH}, decision=decision, actor=actor, digest=digest)
    assert canonical_json_bytes(card.to_dict()) == before


def test_display_is_opt_in_and_shows_real_whitelisted_before_after():
    card = original()
    member = build(card, {GITHUB: GH})
    value = group_candidate_value([member])
    key = (subject_hash_for(ACTOR), card.access_id)
    originals = {key: card.to_dict()}
    ordinary = plan_display(value, originals)
    assert "properties" not in ordinary[0]["before"] and "properties" not in ordinary[0]["after"]
    shown = plan_display(value, originals, person_settings={key: (PROJECT, ACTOR)})
    assert shown[0]["before"]["properties"] == {EMAIL: card.properties[EMAIL]}
    assert shown[0]["after"]["properties"][GITHUB]["account_id"] == GH["account_id"]
    for part in (shown[0]["before"], shown[0]["after"]):
        assert "kept" not in part["properties"]
    with pytest.raises(DecisionRefused):
        plan_display(value, originals, person_settings={key: (PROJECT, "synthetic-admin")})

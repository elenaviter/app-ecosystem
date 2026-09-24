# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONTROL
from connection_hub.delegated_credentials.cards.model import (
    CREDENTIALLESS_CARD_SOURCE,
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    PROJECT_PERSON_CONTROL_AUDIT_SCHEMA,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlAudit,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
    bind_project_person_control,
    project_authority_subject,
    project_person_control_id,
)


PROJECT_REF = "work:project:quickstart"
TARGET = "platform-user-2"
RESOURCE = "https://example.test/mcp/problem_board"


def _authority(*, revision: int = 1, operations: tuple[str, ...] = ("review.accept",)) -> CardAuthority:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    return CardAuthority(
        access_id=identity.control_id,
        client_id="control-card:project",
        grantor_subject=identity.project_subject,
        delegate_subject="",
        source=CREDENTIALLESS_CARD_SOURCE,
        card_kind=CARD_KIND_CONTROL,
        label="Quickstart member",
        card_revision=revision,
        catalog_version="catalog-v1",
        resource_grants={RESOURCE: ("work:review",)},
        resource_operations={RESOURCE: operations},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        issuer_ref=PROJECT_REF,
        issuer_kind="project",
        composition_mode="and",
    )


def test_project_partition_and_card_id_are_stable_and_target_specific() -> None:
    first = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    same = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    other = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject="platform-user-3",
    )

    assert first == same
    assert first.project_subject == other.project_subject == project_authority_subject(PROJECT_REF)
    assert first.control_id == project_person_control_id(PROJECT_REF, TARGET)
    assert first.control_id != other.control_id
    assert TARGET not in first.project_subject
    assert TARGET not in first.control_id


def test_binding_names_the_target_but_keeps_the_project_as_holder() -> None:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )

    bound = bind_project_person_control(_authority(), identity=identity)

    assert bound.grantor_subject == identity.project_subject
    assert bound.grantor_subject != TARGET
    assert bound.issuer_ref == PROJECT_REF
    assert bound.properties[PROJECT_PERSON_CONTROL_PROPERTY]["target_subject"] == TARGET
    assert ProjectPersonControlIdentity.from_authority(bound) == identity


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"grantor_subject": TARGET}, "project_person_control_grantor_mismatch"),
        ({"access_id": "person-control-wrong"}, "project_person_control_id_mismatch"),
        ({"issuer_ref": "work:project:other"}, "project_person_control_issuer_ref_mismatch"),
    ],
)
def test_identity_tampering_is_refused(change: dict[str, str], reason: str) -> None:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    bound = bind_project_person_control(_authority(), identity=identity)

    with pytest.raises(ProjectPersonControlError, match=reason):
        ProjectPersonControlIdentity.from_authority(dataclasses.replace(bound, **change))


def test_audit_records_actor_time_request_and_exact_revision_diff() -> None:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    before = bind_project_person_control(_authority(), identity=identity)
    candidate = bind_project_person_control(
        dataclasses.replace(
            before,
            card_revision=2,
            resource_operations={RESOURCE: ("review.accept", "review.return")},
        ),
        identity=identity,
    )
    audit = ProjectPersonControlAudit.build(
        action="updated",
        actor_subject="platform-admin-1",
        identity=identity,
        request_id="request-123",
        occurred_at=1_790_200_000,
        before=before,
        after=candidate,
    )

    updated = bind_project_person_control(candidate, identity=identity, audit=audit)
    evidence = updated.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]

    assert evidence["schema"] == PROJECT_PERSON_CONTROL_AUDIT_SCHEMA
    assert evidence["actor_subject"] == "platform-admin-1"
    assert evidence["request_id"] == "request-123"
    assert evidence["before_revision"] == 1
    assert evidence["after_revision"] == 2
    assert evidence["changes"] == {
        "resource_operations": {
            "before": {RESOURCE: ["review.accept"]},
            "after": {RESOURCE: ["review.accept", "review.return"]},
        }
    }
    assert PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE not in before.provenance


def test_audit_refuses_an_event_without_an_authority_change() -> None:
    identity = ProjectPersonControlIdentity.build(
        project_ref=PROJECT_REF,
        target_subject=TARGET,
    )
    before = bind_project_person_control(_authority(), identity=identity)
    unchanged_revision = dataclasses.replace(before, card_revision=2)

    with pytest.raises(
        ProjectPersonControlError,
        match="project_person_control_audit_changes_empty",
    ):
        ProjectPersonControlAudit.build(
            action="updated",
            actor_subject="platform-admin-1",
            identity=identity,
            request_id="request-unchanged",
            occurred_at=1_790_200_000,
            before=before,
            after=unchanged_revision,
        )

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_UPDATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationRequest,
)


def _request(*, operation: str = PROJECT_PERSON_CONTROL_UPDATE) -> ProjectAuthorizationRequest:
    return ProjectAuthorizationRequest.build(
        actor_subject="platform-admin-1",
        project_ref="work:project:quickstart",
        target_subject="platform-user-2",
        operation=operation,
        request_id="request-123",
    )


def test_allow_decision_is_bound_to_exact_trusted_coordinates() -> None:
    request = _request()
    decision = ProjectAuthorizationDecision.allow(
        request,
        delegable_grants=["work:review", "work:admin", "work:review"],
        platform_admin=True,
        evidence={"membership_revision": 7},
    )

    decision.validate_for(request)

    assert decision.delegable_grants == ("work:admin", "work:review")
    assert decision.evidence == {"membership_revision": 7}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"actor_subject": "platform-admin-2"}, "project_authorization_actor_mismatch"),
        ({"project_ref": "work:project:other"}, "project_authorization_project_ref_mismatch"),
        ({"target_subject": "platform-user-3"}, "project_authorization_target_mismatch"),
        ({"operation": PROJECT_PERSON_CONTROL_CREATE}, "project_authorization_operation_mismatch"),
        ({"request_id": "request-other"}, "project_authorization_request_id_mismatch"),
    ],
)
def test_decision_for_other_coordinates_is_refused(
    change: dict[str, object],
    reason: str,
) -> None:
    request = _request()
    decision = dataclasses.replace(
        ProjectAuthorizationDecision.allow(request),
        **change,
    )

    with pytest.raises(ProjectAuthorizationError, match=reason):
        decision.validate_for(request)


def test_denial_requires_a_named_reason() -> None:
    request = _request()

    with pytest.raises(
        ProjectAuthorizationError,
        match="project_authorization_denial_reason_missing",
    ):
        ProjectAuthorizationDecision.deny(request, reason="")


def test_malformed_decision_authority_is_refused() -> None:
    request = _request()
    decision = dataclasses.replace(
        ProjectAuthorizationDecision.allow(request),
        platform_admin="yes",
    )

    with pytest.raises(
        ProjectAuthorizationError,
        match="project_authorization_admin_flag_invalid",
    ):
        decision.validate_for(request)


def test_request_has_no_role_or_creator_authority_input() -> None:
    request = _request(operation=PROJECT_PERSON_CONTROL_CREATE)

    assert set(dataclasses.asdict(request)) == {
        "actor_subject",
        "project_ref",
        "target_subject",
        "operation",
        "request_id",
    }

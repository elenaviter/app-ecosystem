# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses

import pytest
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_INVITATION_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_CREATE,
    PROJECT_PERSON_CONTROL_READ,
    PROJECT_PERSON_CONTROL_REVOKE,
    PROJECT_PERSON_CONTROL_UPDATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationRequest,
    ProjectMembershipConfig,
    ProjectMembershipEvidence,
    ResolverBackedProjectAuthorizationPort,
)

PROJECT_REF = "work:project:quickstart"
ADMIN = "platform-admin-1"
TARGET = "platform-user-2"


class _MembershipResolver:
    def __init__(self, memberships: dict[tuple[str, str], object]) -> None:
        self.memberships = memberships
        self.calls: list[tuple[str, str]] = []

    async def resolve_project_membership(self, *, project_ref: str, subject: str):
        self.calls.append((project_ref, subject))
        return self.memberships.get((project_ref, subject))


def _membership(
    subject: str,
    *,
    role: str,
    grants: tuple[str, ...] = (),
) -> ProjectMembershipEvidence:
    return ProjectMembershipEvidence.build(
        project_ref=PROJECT_REF,
        subject=subject,
        role=role,
        delegable_grants=grants,
        evidence={"membership_revision": 7},
    )


def _port(resolver) -> ResolverBackedProjectAuthorizationPort:
    return ResolverBackedProjectAuthorizationPort(
        resolver=resolver,
        administrative_roles={"owner", "admin"},
    )


def _request(
    *, operation: str = PROJECT_PERSON_CONTROL_UPDATE
) -> ProjectAuthorizationRequest:
    return ProjectAuthorizationRequest.build(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=TARGET,
        operation=operation,
        request_id="request-123",
    )


def test_project_membership_config_reads_provider_and_roles() -> None:
    config = ProjectMembershipConfig.from_mapping(
        {
            "provider": {
                "bundle_id": "problem-board@1-0",
                "operation": "project_membership_resolve",
            },
            "administrative_roles": ["owner", "admin", "OWNER"],
        }
    )

    assert config.provider_configured is True
    assert config.provider_bundle_id == "problem-board@1-0"
    assert config.provider_operation == "project_membership_resolve"
    assert config.administrative_roles == ("admin", "owner")


def test_project_membership_config_keeps_missing_provider_explicit() -> None:
    config = ProjectMembershipConfig.from_mapping(None)

    assert config.provider_configured is False
    assert config.administrative_roles == ()


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "problem-board@1-0"},
        {"provider": {"bundle_id": "problem-board@1-0"}},
        {"provider": {"operation": "project_membership_resolve"}},
    ],
)
def test_project_membership_config_refuses_malformed_provider(value) -> None:
    with pytest.raises(ProjectAuthorizationError, match="project_membership_provider_invalid"):
        ProjectMembershipConfig.from_mapping(value)


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
        (
            {"project_ref": "work:project:other"},
            "project_authorization_project_ref_mismatch",
        ),
        (
            {"target_subject": "platform-user-3"},
            "project_authorization_target_mismatch",
        ),
        (
            {"operation": PROJECT_PERSON_CONTROL_CREATE},
            "project_authorization_operation_mismatch",
        ),
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


@pytest.mark.parametrize(
    "delegable_grants",
    ({"work:review": True}, object()),
    ids=("mapping", "object"),
)
def test_membership_rejects_malformed_delegable_grants(delegable_grants) -> None:
    with pytest.raises(
        ProjectAuthorizationError,
        match="project_authorization_grants_invalid",
    ):
        ProjectMembershipEvidence.build(
            project_ref=PROJECT_REF,
            subject=ADMIN,
            role="admin",
            delegable_grants=delegable_grants,
        )


@pytest.mark.parametrize(
    "administrative_roles",
    ({"admin": True}, object()),
    ids=("mapping", "object"),
)
def test_port_rejects_malformed_administrative_roles(administrative_roles) -> None:
    with pytest.raises(
        ProjectAuthorizationError,
        match="project_administrative_roles_invalid",
    ):
        ResolverBackedProjectAuthorizationPort(
            resolver=_MembershipResolver({}),
            administrative_roles=administrative_roles,
        )


@pytest.mark.asyncio
async def test_resolver_port_allows_a_project_admin_with_exact_membership_evidence() -> (
    None
):
    resolver = _MembershipResolver(
        {
            (PROJECT_REF, ADMIN): _membership(
                ADMIN,
                role="admin",
                grants=("work:review", "work:coordinate"),
            ),
            (PROJECT_REF, TARGET): _membership(TARGET, role="member"),
        }
    )

    decision = await _port(resolver).authorize_project_person_control(_request())

    assert decision.allowed
    assert decision.delegable_grants == ("work:coordinate", "work:review")
    assert not decision.platform_admin
    assert decision.evidence["authorization_source"] == "project_membership_resolver"
    assert decision.evidence["actor_membership"]["role"] == "admin"
    assert decision.evidence["target_membership"]["role"] == "member"


@pytest.mark.asyncio
async def test_resolver_port_denies_a_non_admin_before_resolving_the_target() -> None:
    resolver = _MembershipResolver(
        {
            (PROJECT_REF, ADMIN): _membership(ADMIN, role="member"),
            (PROJECT_REF, TARGET): _membership(TARGET, role="member"),
        }
    )

    decision = await _port(resolver).authorize_project_person_control(_request())

    assert not decision.allowed
    assert decision.reason == "project_actor_role_not_administrative"
    assert resolver.calls == [(PROJECT_REF, ADMIN)]


@pytest.mark.asyncio
async def test_project_admin_can_revoke_after_target_membership_is_removed() -> None:
    resolver = _MembershipResolver(
        {(PROJECT_REF, ADMIN): _membership(ADMIN, role="admin")}
    )

    decision = await _port(resolver).authorize_project_person_control(
        _request(operation=PROJECT_PERSON_CONTROL_REVOKE)
    )

    assert decision.allowed
    assert resolver.calls == [(PROJECT_REF, ADMIN)]
    assert "target_membership" not in decision.evidence


@pytest.mark.asyncio
async def test_project_admin_can_create_a_pending_invitation_before_membership_exists() -> None:
    resolver = _MembershipResolver(
        {(PROJECT_REF, ADMIN): _membership(ADMIN, role="admin")}
    )
    request = ProjectAuthorizationRequest.build(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject="work:invitation:inv-1",
        operation=PROJECT_INVITATION_CONTROL_CREATE,
        request_id="invitation-create",
    )

    decision = await _port(resolver).authorize_project_person_control(request)

    assert decision.allowed
    assert resolver.calls == [(PROJECT_REF, ADMIN)]
    assert "target_membership" not in decision.evidence


@pytest.mark.asyncio
async def test_non_admin_cannot_revoke_a_former_members_card() -> None:
    resolver = _MembershipResolver(
        {(PROJECT_REF, ADMIN): _membership(ADMIN, role="member")}
    )

    decision = await _port(resolver).authorize_project_person_control(
        _request(operation=PROJECT_PERSON_CONTROL_REVOKE)
    )

    assert not decision.allowed
    assert decision.reason == "project_actor_role_not_administrative"
    assert resolver.calls == [(PROJECT_REF, ADMIN)]


@pytest.mark.asyncio
async def test_resolver_port_denies_when_no_resolver_is_bound() -> None:
    decision = await _port(None).authorize_project_person_control(_request())

    assert not decision.allowed
    assert decision.reason == "project_membership_resolver_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memberships", "reason"),
    (
        ({}, "project_actor_membership_missing"),
        (
            {(PROJECT_REF, ADMIN): _membership(ADMIN, role="admin")},
            "project_target_membership_missing",
        ),
    ),
)
async def test_resolver_port_denies_missing_membership_by_name(
    memberships: dict[tuple[str, str], object],
    reason: str,
) -> None:
    decision = await _port(
        _MembershipResolver(memberships)
    ).authorize_project_person_control(_request())

    assert not decision.allowed
    assert decision.reason == reason


@pytest.mark.asyncio
async def test_project_owner_can_create_their_own_creator_bootstrap_card() -> None:
    owner = _membership(
        ADMIN,
        role="owner",
        grants=("work:admin",),
    )
    resolver = _MembershipResolver({(PROJECT_REF, ADMIN): owner})
    request = ProjectAuthorizationRequest.build(
        actor_subject=ADMIN,
        project_ref=PROJECT_REF,
        target_subject=ADMIN,
        operation=PROJECT_PERSON_CONTROL_CREATE,
        request_id="creator-bootstrap",
    )

    decision = await _port(resolver).authorize_project_person_control(request)

    assert decision.allowed
    assert decision.delegable_grants == ("work:admin",)
    assert resolver.calls == [(PROJECT_REF, ADMIN)]


@pytest.mark.asyncio
async def test_mismatched_membership_evidence_is_a_named_denial() -> None:
    wrong_actor = dataclasses.replace(
        _membership(ADMIN, role="admin"),
        project_ref="work:project:other",
    )
    resolver = _MembershipResolver({(PROJECT_REF, ADMIN): wrong_actor})

    decision = await _port(resolver).authorize_project_person_control(_request())

    assert not decision.allowed
    assert decision.reason == "project_membership_project_ref_mismatch"


# -- the operator's rule for a person's own Control Card (W260, 2026-09-26) --


def _own(subject: str, operation: str) -> ProjectAuthorizationRequest:
    return ProjectAuthorizationRequest.build(
        actor_subject=subject,
        project_ref=PROJECT_REF,
        target_subject=subject,
        operation=operation,
        request_id="request-own",
    )


@pytest.mark.asyncio
async def test_a_member_reads_their_own_control_card_and_nothing_else() -> None:
    member = _membership(TARGET, role="member", grants=("work:review",))
    port = _port(_MembershipResolver({(PROJECT_REF, TARGET): member}))

    own = await port.authorize_project_person_control(_own(TARGET, PROJECT_PERSON_CONTROL_READ))
    assert own.allowed is True
    assert own.delegable_grants == ("work:review",)
    assert own.evidence["own_card"] is True

    other = ProjectAuthorizationRequest.build(
        actor_subject=TARGET, project_ref=PROJECT_REF, target_subject=ADMIN,
        operation=PROJECT_PERSON_CONTROL_READ, request_id="request-other",
    )
    refused = await port.authorize_project_person_control(other)
    assert refused.allowed is False and refused.reason == "project_actor_role_not_administrative"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [PROJECT_PERSON_CONTROL_UPDATE, PROJECT_PERSON_CONTROL_REVOKE])
async def test_a_members_own_control_card_is_decided_by_an_admin(operation) -> None:
    member = _membership(TARGET, role="member")
    port = _port(_MembershipResolver({(PROJECT_REF, TARGET): member}))
    refused = await port.authorize_project_person_control(_own(TARGET, operation))
    assert refused.allowed is False and refused.reason == "project_person_control_decided_by_admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [PROJECT_PERSON_CONTROL_READ, PROJECT_PERSON_CONTROL_UPDATE, PROJECT_PERSON_CONTROL_REVOKE])
async def test_a_project_admin_does_anything_to_their_own_control_card(operation) -> None:
    """The board's Team > People writes an admin's own Card under that admin's session."""

    admin = _membership(ADMIN, role="admin", grants=("work:admin",))
    port = _port(_MembershipResolver({(PROJECT_REF, ADMIN): admin}))
    allowed = await port.authorize_project_person_control(_own(ADMIN, operation))
    assert allowed.allowed is True and allowed.delegable_grants == ("work:admin",)


@pytest.mark.asyncio
async def test_a_person_who_is_not_a_member_reads_nothing() -> None:
    port = _port(_MembershipResolver({}))
    refused = await port.authorize_project_person_control(_own(TARGET, PROJECT_PERSON_CONTROL_READ))
    assert refused.allowed is False and refused.reason == "project_actor_membership_missing"

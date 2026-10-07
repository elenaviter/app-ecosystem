"""W607: existing C/My PLAN candidates and truthful public before images."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.card_lifecycle_plan import (
    build_application_control,
    build_project_person_control,
)
from connection_hub.delegated_credentials.cards.card_group import group_candidate_value, group_member
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED, CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.service import replace_state
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.existing_card_selection_plan import (
    build_existing_card_selection_update,
)
from connection_hub.delegated_credentials.plan_display import plan_display
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_UPDATE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card


PROJECT = "work:project:w607"
ACTOR = "operator-1"
TARGET = "person-1"
REQUEST = "one-business-change"


def _cards() -> tuple[CardAuthority, CardAuthority, CardAuthority]:
    project = build_application_control(
        project_ref=PROJECT, holder_subject=ACTOR, catalog_version="catalog-v1", now=100,
    )
    control = build_project_person_control(
        identity=ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=TARGET),
        catalog_version="catalog-v1", actor_subject=ACTOR, request_id=REQUEST,
        parent=project, now=100,
    )
    my_card = new_project_person_my_card(control_card=control, now=100)
    return project, control, my_card


def _decision(*, allowed: bool = True, target: str = TARGET, request_id: str = REQUEST,
              grants: tuple[str, ...] = ("work:admin",)) -> ProjectAuthorizationDecision:
    request = ProjectAuthorizationRequest.build(
        actor_subject=ACTOR, project_ref=PROJECT, target_subject=target,
        operation=PROJECT_PERSON_CONTROL_UPDATE, request_id=request_id,
    )
    return (ProjectAuthorizationDecision.allow(request, delegable_grants=grants, platform_admin=True)
            if allowed else ProjectAuthorizationDecision.deny(request, reason="not_allowed"))


class _Host:
    def __init__(self) -> None:
        self.calls = []
        self.writes = 0
        self.revoke = False

    async def _resolve_card_authority(self, **kwargs):
        self.calls.append(kwargs)
        grants = {resource: tuple(values) for resource, values in kwargs["resource_grants"].items()}
        operations = {resource: tuple(values) for resource, values in (kwargs["resource_operations"] or {}).items()}
        account_scope = kwargs["account_scope"] or {}
        return SimpleNamespace(
            error=None, revoke=self.revoke, resource_grants=grants, resource_operations=operations,
            operations=tuple(sorted({operation for values in operations.values() for operation in values})),
            named_service_operations=NamedServiceSelection.none(), named_services={},
            account_scope=account_scope, identity_scope=kwargs["existing"].identity_scope,
            properties=kwargs["properties"],
        )

    @staticmethod
    def _version_of(active):
        return active.version

    async def _catalog_config(self, active, *, owner_subject):
        return {"owner": owner_subject}

    @staticmethod
    def _configured_resource(resource, *, config):
        return None


async def _propose(host: _Host, original: CardAuthority, selection=None):
    return await build_existing_card_selection_update(
        host, original=original, selection=selection or {"resource_grants": {"service-a": ["work:admin"]}},
        active=SimpleNamespace(version="catalog-v2"), decision=_decision(),
        project_ref=PROJECT, target_subject=TARGET, actor_subject=ACTOR,
        request_id=REQUEST, now=200,
    )


def test_person_control_proposal_keeps_identity_parent_and_credentialless_shape() -> None:
    _, control, _ = _cards()
    host = _Host()
    result = asyncio.run(_propose(host, control))
    member = result["member"]
    candidate = CardAuthority.from_mapping(member["candidate"])
    assert host.writes == 0
    assert member["action"] == "update"
    assert member["original_revision"] == control.card_revision
    assert candidate.card_revision == control.card_revision + 1
    assert candidate.access_id == control.access_id
    assert candidate.grantor_subject == control.grantor_subject
    assert candidate.expires_at == control.expires_at
    assert candidate.control_card == control.control_card
    assert candidate.resource_grants == {"service-a": ("work:admin",)}
    assert candidate.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]["request_id"] == REQUEST
    assert host.calls[0]["_delegable_grants"] == ("work:admin",)


def test_my_card_proposal_preserves_unmentioned_dimensions_and_parent() -> None:
    _, _, my_card = _cards()
    original = dataclasses.replace(my_card, account_scope={"calendar": {"acct": ("read",)}})
    host = _Host()
    result = asyncio.run(_propose(host, original))
    candidate = CardAuthority.from_mapping(result["member"]["candidate"])
    assert candidate.account_scope == original.account_scope
    assert candidate.control_card == original.control_card
    assert candidate.expires_at == original.expires_at
    assert host.calls[0]["account_scope"] == original.account_scope


def test_wrong_project_target_revoked_or_denied_never_proposes() -> None:
    _, control, _ = _cards()
    host = _Host()
    for original, decision, scope in (
        (control, _decision(), "work:project:other"),
        (control, _decision(target="other-person"), PROJECT),
        (dataclasses.replace(control, state=CARD_STATE_REVOKED), _decision(), PROJECT),
        (control, _decision(allowed=False), PROJECT),
    ):
        with pytest.raises(DecisionRefused):
            asyncio.run(build_existing_card_selection_update(
                host, original=original, selection={"resource_grants": {"service-a": ["work:admin"]}},
                active=SimpleNamespace(version="catalog-v2"), decision=decision,
                project_ref=scope, target_subject=TARGET, actor_subject=ACTOR,
                request_id=REQUEST, now=200,
            ))
    assert host.writes == 0


def test_decision_for_another_request_cannot_authorize_this_proposal() -> None:
    _, control, _ = _cards()
    with pytest.raises(DecisionRefused, match="card_plan_authorization_invalid"):
        asyncio.run(build_existing_card_selection_update(
            _Host(), original=control,
            selection={"resource_grants": {"service-a": ["work:admin"]}},
            active=SimpleNamespace(version="catalog-v2"),
            decision=_decision(request_id="another-business-change"),
            project_ref=PROJECT, target_subject=TARGET, actor_subject=ACTOR,
            request_id=REQUEST, now=200,
        ))


def test_other_project_control_identity_refuses_even_with_local_parent() -> None:
    # Keep the parent local so only the Card's own project identity can reject
    # this cross-project target. A parent-only scope check would miss it.
    project, _, _ = _cards()
    foreign_control = build_project_person_control(
        identity=ProjectPersonControlIdentity.build(
            project_ref="work:project:other", target_subject=TARGET,
        ),
        catalog_version="catalog-v1", actor_subject=ACTOR,
        request_id=REQUEST, parent=project, now=100,
    )
    with pytest.raises(DecisionRefused, match="card_plan_update_scope_invalid"):
        asyncio.run(_propose(_Host(), foreign_control))


def test_empty_noop_and_malformed_selection_refuse_without_revoke() -> None:
    _, control, my_card = _cards()
    host = _Host()
    host.revoke = True
    with pytest.raises(DecisionRefused, match="card_plan_selection_empty"):
        asyncio.run(_propose(host, control))
    host.revoke = False
    for original in (control, my_card):
        with pytest.raises(DecisionRefused, match="card_plan_selection_unchanged"):
            asyncio.run(build_existing_card_selection_update(
                host, original=original, selection={}, active=SimpleNamespace(version="catalog-v1"),
                decision=_decision(), project_ref=PROJECT, target_subject=TARGET,
                actor_subject=ACTOR, request_id=REQUEST, now=200,
            ))
    with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
        asyncio.run(_propose(host, control, selection={"properties": ["not a mapping"]}))
    assert host.writes == 0


def test_supplied_dimension_replaces_whole_map_and_preserves_other_dimensions() -> None:
    _, _, my_card = _cards()
    original = dataclasses.replace(
        my_card,
        resource_grants={"service-a": ("read",), "unrelated": ("read",)},
        account_scope={"calendar": {"acct": ("read",)}},
    )
    result = asyncio.run(_propose(
        _Host(), original, selection={"resource_grants": {"service-a": ["work:admin"]}},
    ))
    candidate = CardAuthority.from_mapping(result["member"]["candidate"])
    assert candidate.resource_grants == {"service-a": ("work:admin",)}
    assert candidate.account_scope == original.account_scope
    key = (result["member"]["subject_hash"], result["member"]["access_id"])
    display = plan_display(group_candidate_value([result["member"]]), {key: original.to_dict()})[0]
    assert "unrelated" in display["before"]["resource_grants"]
    assert "unrelated" not in display["after"]["resource_grants"]


def test_live_catalog_resolver_accepts_card_authority_and_enforces_decision_ceiling() -> None:
    # The helper passes CardAuthority directly to the shared resolver. This
    # exercises that real seam, rather than making a fake resolver responsible
    # for the role-derived ceiling.
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections

    connections = {"delegated_credentials": {"oauth": {
        "enabled": True,
        "capabilities": [
            {"grant": "work:observe", "label": "Observe", "delegable_roles": ["kdcube:role:registered"]},
        ],
        "resources": [{"resource": "problem_board", "label": "Problem Board", "tools": {
            "work.report": {"label": "Report", "grants": ["work:observe"]},
        }}],
    }}}
    host = AutomationAccessService(
        redis=object(), tenant="tenant", project="project",
        config=oauth_delegated_config_from_connections(connections),
    )
    _, control, _ = _cards()
    selection = {"resource_grants": {"problem_board": ["work:observe"]},
                 "resource_operations": {"problem_board": ["work.report"]}}
    active = SimpleNamespace(version="catalog-v2", connections=connections)
    result = asyncio.run(build_existing_card_selection_update(
        host, original=control, selection=selection, active=active,
        decision=_decision(grants=("work:observe",)), project_ref=PROJECT,
        target_subject=TARGET, actor_subject=ACTOR, request_id=REQUEST, now=200,
    ))
    candidate = CardAuthority.from_mapping(result["member"]["candidate"])
    assert candidate.resource_grants == {"problem_board": ("work:observe",)}
    assert candidate.resource_operations == {"problem_board": ("work.report",)}
    with pytest.raises(DecisionRefused, match="card_plan_selection_unchanged"):
        asyncio.run(build_existing_card_selection_update(
            host, original=candidate, selection=selection, active=active,
            decision=_decision(grants=("work:observe",)), project_ref=PROJECT,
            target_subject=TARGET, actor_subject=ACTOR, request_id=REQUEST, now=201,
        ))
    with pytest.raises(DecisionRefused, match="delegated_access_grants_not_delegable"):
        asyncio.run(build_existing_card_selection_update(
            host, original=control, selection=selection, active=active,
            decision=_decision(grants=()), project_ref=PROJECT,
            target_subject=TARGET, actor_subject=ACTOR, request_id=REQUEST, now=200,
        ))


def test_display_uses_exact_loaded_originals_and_sorts_group() -> None:
    project, control, my_card = _cards()
    changed_control = dataclasses.replace(control, card_revision=control.card_revision + 1,
                                          resource_grants={"service-a": ("work:admin",)})
    changed_my = dataclasses.replace(my_card, card_revision=my_card.card_revision + 1,
                                     resource_grants={"service-b": ("read",)})
    members = [
        group_member(original=my_card, candidate=changed_my, action="update"),
        group_member(original=control, candidate=changed_control, action="update"),
        group_member(original=None, candidate=project, action="create"),
    ]
    originals = {
        (subject_hash_for(control.grantor_subject), control.access_id): control.to_dict(),
        (subject_hash_for(my_card.grantor_subject), my_card.access_id): my_card.to_dict(),
        (subject_hash_for(project.grantor_subject), project.access_id): None,
    }
    display = plan_display(group_candidate_value(members), originals)
    assert [entry["access_id"] for entry in display] == [
        member["access_id"] for member in group_candidate_value(members)["cards"]
    ]
    control_entry = next(entry for entry in display if entry["access_id"] == control.access_id)
    assert control_entry["before"]["resource_grants"] == {}
    assert control_entry["after"]["resource_grants"] == {"service-a": ["work:admin"]}
    assert control_entry["before"]["parent"] == control.control_card.to_dict()
    assert next(entry for entry in display if entry["access_id"] == project.access_id)["before"] is None


def test_display_refuses_missing_stale_or_wrong_original() -> None:
    _, control, _ = _cards()
    candidate = dataclasses.replace(control, card_revision=control.card_revision + 1)
    member = group_member(original=control, candidate=candidate, action="update")
    value = group_candidate_value([member])
    key = (member["subject_hash"], member["access_id"])
    with pytest.raises(DecisionRefused):
        plan_display(value, {})
    with pytest.raises(DecisionRefused):
        plan_display(value, {key: None})
    with pytest.raises(DecisionRefused):
        plan_display(value, {key: dataclasses.replace(control, card_revision=99).to_dict()})
    with pytest.raises(DecisionRefused):
        plan_display(value, {key: dataclasses.replace(control, access_id="other").to_dict()})


def test_display_includes_invitation_control_revoke_and_rejects_unknown_kinds() -> None:
    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invitation-1", target_email="person@example.test",
    )
    pending = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=identity.project_subject, catalog_version="catalog-v1",
        control_id=identity.control_id, issuer_ref=identity.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=100,
    ), identity=identity)
    member = group_member(original=pending, candidate=replace_state(pending, CARD_STATE_REVOKED),
                          action="revoke")
    key = (member["subject_hash"], member["access_id"])
    display = plan_display(group_candidate_value([member]), {key: pending.to_dict()})[0]
    assert display["kind"] == "invitation_control"
    assert display["action"] == "revoke"
    assert display["before"]["state"] == pending.state
    assert display["after"]["state"] == CARD_STATE_REVOKED
    assert display["before"]["resource_grants"] == display["after"]["resource_grants"]

    unknown = dataclasses.replace(pending, issuer_kind="unrecognised",
                                  card_revision=pending.card_revision + 1)
    unknown_member = group_member(original=pending, candidate=unknown, action="update")
    with pytest.raises(DecisionRefused, match="card_plan_display_kind_invalid"):
        plan_display(group_candidate_value([unknown_member]), {key: pending.to_dict()})

    malformed = dataclasses.replace(pending, properties={},
                                    card_revision=pending.card_revision + 1)
    malformed_member = group_member(original=pending, candidate=malformed, action="update")
    with pytest.raises(DecisionRefused, match="card_plan_display_kind_invalid"):
        plan_display(group_candidate_value([malformed_member]), {key: pending.to_dict()})

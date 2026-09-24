from __future__ import annotations

import dataclasses

import pytest
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.authorization import (
    ActiveCatalogCapabilities,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.model import (
    new_credentialless_card,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlIdentity,
    bind_project_person_control,
)
from connection_hub.delegated_credentials.project_identity_authorization import (
    BOUNDARY_CATALOG,
    BOUNDARY_CONTROL_CARD,
    BOUNDARY_EDGE,
    BOUNDARY_MY_CARD,
    PROJECT_IDENTITY_EDGE_SCHEMA,
    ProjectCardResolution,
    ProjectIdentityCardReference,
    ProjectIdentityDelegationEdge,
    ProjectOperationRequest,
    authorize_project_operation,
)

NOW = 1_800_000_000
PERSON = "platform-user-1"
PROJECT_REF = "work:project:demo-project"
CONTROL_IDENTITY = ProjectPersonControlIdentity.build(
    project_ref=PROJECT_REF,
    target_subject=PERSON,
)
PROJECT_SUBJECT = CONTROL_IDENTITY.project_subject
CONTROL_ID = CONTROL_IDENTITY.control_id
RESOURCE = "https://board.example/api/integrations/bundles/demo/problem-board/mcp"
OPERATION = "project.people.list"
GRANT = "work:review"


def _catalog(
    *,
    operations: tuple[str, ...] = (OPERATION,),
    grants: tuple[str, ...] = (GRANT,),
) -> ActiveCatalogCapabilities:
    return ActiveCatalogCapabilities(
        CatalogDocument.build(
            {
                "delegated_credentials": {
                    "oauth": {
                        "resources": [
                            {
                                "resource": RESOURCE,
                                "grants": list(grants),
                                "tools": {
                                    operation: {"grants": list(grants)}
                                    for operation in operations
                                },
                            }
                        ]
                    }
                }
            }
        )
    )


def _my_card(
    *,
    operations: tuple[str, ...] = (OPERATION,),
    grants: tuple[str, ...] = (GRANT,),
    revision: int = 7,
    state: str = CARD_STATE_ACTIVE,
    linked: bool = True,
) -> CardAuthority:
    return CardAuthority(
        access_id="my-card-1",
        client_id="project-person:demo-project:platform-user-1",
        grantor_subject=PERSON,
        delegate_subject="session:platform-user-1",
        source="manual",
        card_kind=CARD_KIND_AUTOMATION,
        label="My Card",
        card_revision=revision,
        catalog_version="catalog-before",
        state=state,
        resource_grants={RESOURCE: grants},
        resource_operations={RESOURCE: operations},
        named_service_operations=NamedServiceSelection.none(),
        control_card=(
            ControlCardBinding(
                control_id=CONTROL_ID,
                issuer_ref=PROJECT_REF,
                issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
                control_revision=4,
            )
            if linked
            else None
        ),
        created_at=NOW - 60,
        expires_at=NOW + 3600,
    )


def _control_card(
    *,
    operations: tuple[str, ...] = (OPERATION,),
    grants: tuple[str, ...] = (GRANT,),
    revision: int = 4,
    state: str = CARD_STATE_ACTIVE,
) -> CardAuthority:
    control = new_credentialless_card(
        initial_selection=_my_card(operations=operations, grants=grants),
        grantor_subject=PROJECT_SUBJECT,
        catalog_version="catalog-before",
        control_id=CONTROL_ID,
        issuer_ref=PROJECT_REF,
        issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
        issuer_label="Demo project: person",
        composition_mode="and",
        revision=revision,
        now=NOW - 60,
    )
    return bind_project_person_control(
        dataclasses.replace(control, state=state),
        identity=CONTROL_IDENTITY,
    )


def _edge(
    control: CardAuthority,
    my_card: CardAuthority,
) -> ProjectIdentityDelegationEdge:
    return ProjectIdentityDelegationEdge(
        edge_ref="project-person-edge-1",
        person_subject=PERSON,
        project_ref=PROJECT_REF,
        project_subject=PROJECT_SUBJECT,
        control_card=ProjectIdentityCardReference(
            access_id=control.access_id,
            grantor_subject=control.grantor_subject,
            card_revision=control.card_revision,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
        ),
        my_card=ProjectIdentityCardReference(
            access_id=my_card.access_id,
            grantor_subject=my_card.grantor_subject,
            card_revision=my_card.card_revision,
            issuer_ref=my_card.issuer_ref,
            issuer_kind=my_card.issuer_kind,
        ),
    )


def _request(**changes) -> ProjectOperationRequest:
    values = {
        "person_subject": PERSON,
        "project_ref": PROJECT_REF,
        "resource": RESOURCE,
        "operation": OPERATION,
        "required_grants": (GRANT,),
    }
    values.update(changes)
    return ProjectOperationRequest(**values)


def _authorize(
    *,
    request: ProjectOperationRequest | None = None,
    edge: ProjectIdentityDelegationEdge | None = None,
    control: CardAuthority | None = None,
    my_card: CardAuthority | None = None,
    control_resolution: ProjectCardResolution | None = None,
    my_resolution: ProjectCardResolution | None = None,
    catalog: ActiveCatalogCapabilities | None = None,
):
    selected_control = control or _control_card()
    selected_my_card = my_card or _my_card()
    selected_edge = edge
    if selected_edge is None:
        selected_edge = _edge(selected_control, selected_my_card)
    return authorize_project_operation(
        request=request or _request(),
        edge=selected_edge,
        control_card=(
            control_resolution
            if control_resolution is not None
            else ProjectCardResolution.current(selected_control)
        ),
        my_card=(
            my_resolution
            if my_resolution is not None
            else ProjectCardResolution.current(selected_my_card)
        ),
        catalog=catalog or _catalog(),
        now=NOW,
    )


def test_allow_carries_inspectable_person_project_and_card_edge() -> None:
    decision = _authorize()

    assert decision.allowed
    assert decision.reason == "project_operation_allowed"
    payload = decision.to_dict()
    assert payload["delegation_edge"] == {
        "schema": PROJECT_IDENTITY_EDGE_SCHEMA,
        "edge_ref": "project-person-edge-1",
        "person_identity": {"subject": PERSON},
        "project_identity": {"ref": PROJECT_REF, "subject": PROJECT_SUBJECT},
        "cards": {
            "control_card": {
                "access_id": CONTROL_ID,
                "grantor_subject": PROJECT_SUBJECT,
                "card_revision": 4,
                "issuer_ref": PROJECT_REF,
                "issuer_kind": PROJECT_PERSON_CONTROL_ISSUER_KIND,
            },
            "my_card": {
                "access_id": "my-card-1",
                "grantor_subject": PERSON,
                "card_revision": 7,
            },
        },
    }
    assert payload["resolved_cards"]["control_card"]["card_revision"] == 4
    assert payload["resolved_cards"]["my_card"]["card_revision"] == 7
    assert payload["active_catalog_version"]


@pytest.mark.parametrize(
    ("boundary", "control_operations", "my_operations", "reason"),
    (
        (
            BOUNDARY_CONTROL_CARD,
            (),
            (OPERATION,),
            "control_card_excludes_operation",
        ),
        (BOUNDARY_MY_CARD, (OPERATION,), (), "my_card_excludes_operation"),
    ),
)
def test_each_card_is_an_independent_positive_selection(
    boundary: str,
    control_operations: tuple[str, ...],
    my_operations: tuple[str, ...],
    reason: str,
) -> None:
    control = _control_card(operations=control_operations)
    my_card = _my_card(operations=my_operations)

    decision = _authorize(
        edge=_edge(control, my_card),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == boundary
    assert decision.blocking_capability is not None
    assert decision.blocking_capability.path()["outer_operation"] == OPERATION


@pytest.mark.parametrize(
    ("control_grants", "my_grants", "boundary", "reason"),
    (
        ((), (GRANT,), BOUNDARY_CONTROL_CARD, "control_card_excludes_grant"),
        ((GRANT,), (), BOUNDARY_MY_CARD, "my_card_excludes_grant"),
    ),
)
def test_required_grant_is_checked_on_both_cards(
    control_grants: tuple[str, ...],
    my_grants: tuple[str, ...],
    boundary: str,
    reason: str,
) -> None:
    control = _control_card(grants=control_grants)
    my_card = _my_card(grants=my_grants)

    decision = _authorize(
        edge=_edge(control, my_card),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == boundary
    assert decision.blocking_capability is not None
    assert decision.blocking_capability.path()["claim"] == GRANT


@pytest.mark.parametrize(
    ("role", "resolution", "reason"),
    (
        (
            BOUNDARY_CONTROL_CARD,
            ProjectCardResolution.missing(),
            "control_card_missing",
        ),
        (BOUNDARY_MY_CARD, ProjectCardResolution.missing(), "my_card_missing"),
        (
            BOUNDARY_CONTROL_CARD,
            ProjectCardResolution.updating("mutation_in_progress"),
            "control_card_updating",
        ),
        (
            BOUNDARY_MY_CARD,
            ProjectCardResolution.unavailable("storage_unavailable"),
            "my_card_unavailable",
        ),
    ),
)
def test_missing_or_unavailable_card_resolution_denies_by_name(
    role: str,
    resolution: ProjectCardResolution,
    reason: str,
) -> None:
    kwargs = (
        {"control_resolution": resolution}
        if role == BOUNDARY_CONTROL_CARD
        else {"my_resolution": resolution}
    )

    decision = _authorize(**kwargs)

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == role
    assert decision.retryable is (resolution.state in {"updating", "unavailable"})


@pytest.mark.parametrize(
    ("role", "reason"),
    (
        (BOUNDARY_CONTROL_CARD, "control_card_revoked"),
        (BOUNDARY_MY_CARD, "my_card_revoked"),
    ),
)
def test_revoked_card_denies_by_name(role: str, reason: str) -> None:
    control = _control_card(
        state=CARD_STATE_REVOKED if role == BOUNDARY_CONTROL_CARD else CARD_STATE_ACTIVE
    )
    my_card = _my_card(
        state=CARD_STATE_REVOKED if role == BOUNDARY_MY_CARD else CARD_STATE_ACTIVE
    )

    decision = _authorize(
        edge=_edge(control, my_card),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == role


@pytest.mark.parametrize(
    ("change_marker", "identity_reason"),
    (
        (
            lambda properties: properties.pop(PROJECT_PERSON_CONTROL_PROPERTY),
            "project_person_control_marker_missing",
        ),
        (
            lambda properties: properties[PROJECT_PERSON_CONTROL_PROPERTY].update(
                target_subject="platform-user-other"
            ),
            "project_person_control_id_mismatch",
        ),
    ),
)
def test_control_card_requires_the_exact_project_person_identity(
    change_marker,
    identity_reason: str,
) -> None:
    control = _control_card()
    properties = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in control.properties.items()
    }
    change_marker(properties)
    changed = dataclasses.replace(control, properties=properties)

    decision = _authorize(
        edge=_edge(control, _my_card()),
        control=changed,
    )

    assert not decision.allowed
    assert decision.reason == "control_card_identity_invalid"
    assert decision.blocking_boundary == BOUNDARY_CONTROL_CARD
    assert decision.details == {"identity_reason": identity_reason}


def test_active_catalog_removal_caps_cards_written_against_an_older_catalog() -> None:
    decision = _authorize(catalog=_catalog(operations=()))

    assert not decision.allowed
    assert decision.reason == "active_catalog_excludes_project_operation"
    assert decision.blocking_boundary == BOUNDARY_CATALOG
    assert decision.blocking_capability is not None
    assert decision.blocking_capability.path()["outer_operation"] == OPERATION


def test_active_catalog_denial_precedes_a_stale_stored_card() -> None:
    decision = _authorize(
        catalog=_catalog(operations=()),
        control_resolution=ProjectCardResolution.missing(),
    )

    assert not decision.allowed
    assert decision.reason == "active_catalog_excludes_project_operation"
    assert decision.blocking_boundary == BOUNDARY_CATALOG


@pytest.mark.parametrize(
    ("operation_request", "reason"),
    (
        (
            _request(person_subject="platform-user-2"),
            "project_session_identity_mismatch",
        ),
        (_request(project_ref="work:project:other"), "project_identity_mismatch"),
    ),
)
def test_request_must_match_the_edges_person_and_project(
    operation_request: ProjectOperationRequest,
    reason: str,
) -> None:
    decision = _authorize(request=operation_request)

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == BOUNDARY_EDGE


def test_stale_edge_revision_denies_before_capability_evaluation() -> None:
    control = _control_card()
    my_card = _my_card()
    edge = _edge(control, my_card)
    edge = dataclasses.replace(
        edge,
        my_card=dataclasses.replace(
            edge.my_card,
            card_revision=edge.my_card.card_revision - 1,
        ),
    )

    decision = _authorize(edge=edge, control=control, my_card=my_card)

    assert not decision.allowed
    assert decision.reason == "my_card_revision_mismatch"
    assert decision.blocking_boundary == BOUNDARY_MY_CARD
    assert decision.details == {"resolved_card_revision": my_card.card_revision}


def test_my_card_must_be_linked_to_the_person_control_card() -> None:
    control = _control_card()
    my_card = _my_card(linked=False)

    decision = _authorize(
        edge=_edge(control, my_card),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == "my_card_control_binding_missing"
    assert decision.blocking_boundary == BOUNDARY_MY_CARD


def test_my_card_link_to_another_control_card_is_refused() -> None:
    control = _control_card()
    my_card = dataclasses.replace(
        _my_card(),
        control_card=ControlCardBinding(
            control_id="project-person-control-other",
            issuer_ref=PROJECT_REF,
            issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
            control_revision=4,
        ),
    )

    decision = _authorize(
        edge=_edge(control, my_card),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == "my_card_control_binding_mismatch"
    assert decision.blocking_boundary == BOUNDARY_MY_CARD


@pytest.mark.parametrize(
    ("changed_edge", "reason"),
    (
        (
            lambda edge: dataclasses.replace(
                edge,
                project_subject="project:other",
            ),
            "control_card_project_subject_mismatch",
        ),
        (
            lambda edge: dataclasses.replace(
                edge,
                project_ref="work:project:other",
            ),
            "control_card_project_subject_mismatch",
        ),
        (
            lambda edge: dataclasses.replace(
                edge,
                person_subject="platform-user-2",
            ),
            "control_card_reference_mismatch",
        ),
        (
            lambda edge: dataclasses.replace(
                edge,
                my_card=dataclasses.replace(
                    edge.my_card,
                    grantor_subject="platform-user-2",
                ),
            ),
            "my_card_owner_mismatch",
        ),
    ),
)
def test_edge_binds_each_card_to_the_named_project_and_person(
    changed_edge,
    reason: str,
) -> None:
    control = _control_card()
    my_card = _my_card()

    decision = _authorize(
        edge=changed_edge(_edge(control, my_card)),
        control=control,
        my_card=my_card,
    )

    assert not decision.allowed
    assert decision.reason == reason
    assert decision.blocking_boundary == BOUNDARY_EDGE


def test_missing_edge_is_default_closed() -> None:
    control = _control_card()
    my_card = _my_card()
    decision = authorize_project_operation(
        request=_request(),
        edge=None,
        control_card=ProjectCardResolution.current(control),
        my_card=ProjectCardResolution.current(my_card),
        catalog=_catalog(),
        now=NOW,
    )

    assert not decision.allowed
    assert decision.reason == "project_identity_edge_missing"
    assert decision.blocking_boundary == BOUNDARY_EDGE


@pytest.mark.parametrize(
    "required_grants",
    ({"work:missing": True}, object()),
    ids=("mapping", "object"),
)
def test_malformed_required_grants_are_a_named_default_closed_denial(
    required_grants,
) -> None:
    decision = _authorize(request=_request(required_grants=required_grants))

    assert not decision.allowed
    assert decision.reason == "project_operation_required_grants_invalid"
    assert decision.blocking_boundary == BOUNDARY_EDGE


def test_missing_catalog_is_retryable_and_default_closed() -> None:
    control = _control_card()
    my_card = _my_card()

    decision = authorize_project_operation(
        request=_request(),
        edge=_edge(control, my_card),
        control_card=ProjectCardResolution.current(control),
        my_card=ProjectCardResolution.current(my_card),
        catalog=None,
        now=NOW,
    )

    assert not decision.allowed
    assert decision.reason == "active_catalog_unavailable"
    assert decision.blocking_boundary == BOUNDARY_CATALOG
    assert decision.retryable

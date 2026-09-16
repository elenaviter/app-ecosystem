# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses

from connection_hub.delegated_credentials.cards.model import (
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.authorization import (
    CAPABILITY_OUTER_OPERATION,
    CapabilityRequest,
    CardProvenance,
    card_boundary_denial,
)
from connection_hub.delegated_credentials.controls.attribution import (
    CARD_ROLE_CALLER,
    CARD_ROLE_CONTROL,
    ResolvedCardComposition,
    attribute_card_boundary,
)
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_MODE_EXACT,
    CONTROL_SNAPSHOT_PROPERTY,
    CONTROL_SNAPSHOT_SCHEMA,
    CONTROL_SNAPSHOT_STATE_EXACT,
)

RESOURCE = "https://app.example/mcp"
OWNER = "platform-user-1"


def _control(*, operations: tuple[str, ...], mode: str) -> CardAuthority:
    return CardAuthority(
        access_id="control-card-1",
        client_id="",
        grantor_subject=OWNER,
        delegate_subject="",
        source="control",
        label="Project policy",
        card_revision=7,
        resource_grants={RESOURCE: ("records:read",)},
        resource_operations={RESOURCE: operations},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        issuer_ref="work:project:demo",
        issuer_kind="application",
        issuer_label="Demo project",
        manage_url=(
            "/sites/connections/"
            "?tab=delegatedAccess&control_card_id=control-card-1"
        ),
        composition_mode=mode,
        properties={
            CONTROL_SNAPSHOT_PROPERTY: {
                "schema": CONTROL_SNAPSHOT_SCHEMA,
                "mode": CONTROL_SNAPSHOT_MODE_EXACT,
                "state": CONTROL_SNAPSHOT_STATE_EXACT,
                "basis_catalog_version": "catalog-before",
            }
        },
    )


def _composition(
    *,
    caller_operations: tuple[str, ...],
    control_operations: tuple[str, ...],
    mode: str = CONTROL_COMPOSITION_AND,
) -> ResolvedCardComposition:
    control = _control(operations=control_operations, mode=mode)
    caller = CardAuthority(
        access_id="caller-card-1",
        client_id="kdcube-agent:workspace:main",
        grantor_subject=OWNER,
        delegate_subject=f"integration:agent:{OWNER}",
        source="agent",
        label="Workspace main",
        card_revision=4,
        resource_grants={RESOURCE: ("records:read",)},
        resource_operations={RESOURCE: caller_operations},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        expires_at=4_000_000_000,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            issuer_label=control.issuer_label,
            manage_url=control.manage_url,
            control_revision=control.card_revision,
        ),
    )
    return ResolvedCardComposition(
        caller_card=caller,
        control_card=control,
        effective_card=effective_card_authority(caller, control),
    )


def _permits_records_read(card: CardAuthority) -> bool:
    return "records.read" in card.resource_operations.get(RESOURCE, ())


def test_and_composition_attributes_an_excluded_operation_to_control_only() -> None:
    composition = _composition(
        caller_operations=("records.read",),
        control_operations=(),
    )

    attribution = attribute_card_boundary(
        composition,
        permits=_permits_records_read,
    )

    assert attribution.reliable is True
    assert attribution.control_is_single_blocker is True
    assert [card.role for card in attribution.blocking_cards] == [CARD_ROLE_CONTROL]
    public = attribution.to_public_dict()
    assert public["caller_permits"] is True
    assert public["control_permits"] is False
    assert public["effective_permits"] is False
    assert "manage_url" not in str(public)


def test_and_composition_names_both_cards_when_both_exclude_operation() -> None:
    composition = _composition(
        caller_operations=(),
        control_operations=(),
    )

    attribution = attribute_card_boundary(
        composition,
        permits=_permits_records_read,
    )

    assert attribution.reliable is True
    assert [card.role for card in attribution.blocking_cards] == [
        CARD_ROLE_CALLER,
        CARD_ROLE_CONTROL,
    ]
    assert attribution.single_blocker is None


def test_or_composition_does_not_blame_a_card_when_the_other_side_allows() -> None:
    composition = _composition(
        caller_operations=(),
        control_operations=("records.read",),
        mode=CONTROL_COMPOSITION_OR,
    )

    attribution = attribute_card_boundary(
        composition,
        permits=_permits_records_read,
    )

    assert attribution.reliable is True
    assert attribution.effective_permits is True
    assert attribution.blocking_cards == ()


def test_control_denial_names_control_and_withholds_ungated_editor_url() -> None:
    composition = _composition(
        caller_operations=("records.read",),
        control_operations=(),
    )
    request = CapabilityRequest(
        kind=CAPABILITY_OUTER_OPERATION,
        resource=RESOURCE,
        request_resource=RESOURCE,
        surface="rest",
        outer_operation="records.read",
    )

    denial = card_boundary_denial(
        provenance=CardProvenance(
            access_id=composition.caller_card.access_id,
            card_revision=composition.caller_card.card_revision,
        ),
        request=request,
        card_composition=composition,
    )

    assert denial["error"]["message"] == (
        "The linked Control Card excludes the requested operation."
    )
    assert denial["ret"]["blocking_card"] == {
        "role": "control",
        "access_id": "control-card-1",
        "card_revision": 7,
        "label": "Project policy",
        "issuer_ref": "work:project:demo",
        "issuer_kind": "application",
        "issuer_label": "Demo project",
    }
    assert denial["ret"]["recovery"] == {
        "action": "review_blocking_control_card",
        "retry_same_request": False,
        "request_user_consent": False,
        "edit_route_available": False,
        "target_card_role": "control",
        "target_access_id": "control-card-1",
    }
    assert "control_card_id" not in str(denial)
    assert "edit_url" not in str(denial)


def test_inconsistent_effective_projection_never_routes_to_either_card() -> None:
    composition = _composition(
        caller_operations=("records.read",),
        control_operations=(),
    )
    inconsistent = dataclasses.replace(
        composition,
        effective_card=dataclasses.replace(
            composition.effective_card,
            resource_operations={RESOURCE: ("records.read",)},
        ),
    )

    attribution = attribute_card_boundary(
        inconsistent,
        permits=_permits_records_read,
    )

    assert attribution.reliable is False
    assert attribution.blocking_cards == ()

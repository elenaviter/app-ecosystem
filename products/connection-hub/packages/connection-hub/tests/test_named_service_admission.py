from __future__ import annotations

import dataclasses

import pytest

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.authorization import (
    ActiveCatalogCapabilities,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.attribution import (
    ResolvedCardComposition,
)
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    CONVERSATION_TARGETS_PROPERTY,
    conversation_targets,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_MODE_EXACT,
    CONTROL_SNAPSHOT_PROPERTY,
    CONTROL_SNAPSHOT_SCHEMA,
    CONTROL_SNAPSHOT_STATE_EXACT,
)
from connection_hub.named_service_admission import (
    DELEGATED_CARD_BINDING_SCHEMA,
    NamedServiceAdmissionResolutionError,
    delegated_card_binding,
    evaluate_managed_named_service,
    evaluate_resolved_hub_state,
    snapshot_from_grant,
    validate_relay_selector,
)

RESOURCE = "https://app.example/mcp"


def _catalog(*, include_search: bool = True) -> ActiveCatalogCapabilities:
    operations = {
        "object.search": {"grants": ["records:read"]}
    } if include_search else {
        "object.get": {"grants": ["records:read"]}
    }
    return ActiveCatalogCapabilities(
        CatalogDocument.build(
            {
                "delegated_credentials": {
                    "oauth": {
                        "resources": [
                            {
                                "resource": RESOURCE,
                                "grants": ["records:read"],
                                "tools": {"named_services": {}},
                                "named_services": {
                                    "namespaces": {
                                        "records": {
                                            "tools": {
                                                "objects": {
                                                    "operations": operations
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        )
    )


def _snapshot(catalog: ActiveCatalogCapabilities):
    return snapshot_from_grant(
        catalog=catalog,
        grant_record={
            "registry_access_id": "access-1",
            "client_id": "agent-1",
            "grantor_subject": "user-1",
            "card_revision": 2,
            "catalog_version": "catalog-before",
            "resource_grants": {
                RESOURCE: ["records:read", "records:read:any_user"]
            },
            "named_services": {
                "namespaces": {
                    "records": {
                        "tools": {
                            "objects": {
                                "operations": {
                                    "object.search": {
                                        "grants": ["records:read"]
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "account_scope": {"records": {"account-1": ["records:read"]}},
        },
        credential=CredentialEnvelope(subject="integration:agent:user-1"),
        resource=RESOURCE,
        request_resource=RESOURCE,
        outer_operation="named_services",
    )


def _control_composition() -> ResolvedCardComposition:
    named_services = {
        "namespaces": {
            "records": {
                "tools": {
                    "objects": {
                        "operations": {
                            "object.search": {"grants": ["records:read"]}
                        }
                    }
                }
            }
        }
    }
    control = CardAuthority(
        access_id="control-card-1",
        client_id="",
        grantor_subject="user-1",
        delegate_subject="",
        source="control",
        card_revision=3,
        resource_grants={RESOURCE: ("records:read",)},
        resource_operations={RESOURCE: ("named_services",)},
        named_service_operations=NamedServiceSelection.none(),
        named_services={},
        identity_scope="grantor",
        issuer_ref="work:project:demo",
        issuer_kind="application",
        composition_mode="and",
        properties={
            CONTROL_SNAPSHOT_PROPERTY: {
                "schema": CONTROL_SNAPSHOT_SCHEMA,
                "mode": CONTROL_SNAPSHOT_MODE_EXACT,
                "state": CONTROL_SNAPSHOT_STATE_EXACT,
                "basis_catalog_version": "catalog-before",
            }
        },
    )
    caller = CardAuthority(
        access_id="access-1",
        client_id="agent-1",
        grantor_subject="user-1",
        delegate_subject="integration:agent:user-1",
        source="agent",
        card_revision=2,
        resource_grants={RESOURCE: ("records:read",)},
        resource_operations={RESOURCE: ("named_services",)},
        named_service_operations=NamedServiceSelection.exact(
            {RESOURCE: {"records": ("object.search",)}}
        ),
        named_services=named_services,
        identity_scope="grantor",
        expires_at=4_000_000_000,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            control_revision=control.card_revision,
        ),
    )
    return ResolvedCardComposition(
        caller_card=caller,
        control_card=control,
        effective_card=effective_card_authority(caller, control),
    )


def test_named_service_is_bounded_by_both_card_and_active_catalog() -> None:
    snapshot = _snapshot(_catalog())
    allowed = evaluate_managed_named_service(
        snapshot,
        namespace="records",
        operation="object.search",
    )
    removed = evaluate_managed_named_service(
        _snapshot(_catalog(include_search=False)),
        namespace="records",
        operation="object.search",
    )

    assert allowed.allowed
    assert allowed.account_scope == {
        "records": {"account-1": ["records:read"]}
    }
    assert allowed.claims == ("records:read", "records:read:any_user")
    assert not removed.allowed
    assert removed.denial["error"]["code"] == (
        "delegated_capability_no_longer_available"
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("and", ("shared-app",)), ("or", ("caller-app", "control-app", "shared-app"))],
)
def test_conversation_targets_follow_card_composition(mode: str, expected: tuple[str, ...]) -> None:
    composition = _control_composition()
    caller = dataclasses.replace(
        composition.caller_card,
        properties={CONVERSATION_TARGETS_PROPERTY: ["caller-app", "shared-app"]},
    )
    control = dataclasses.replace(
        composition.control_card,
        composition_mode=mode,
        properties={
            **composition.control_card.properties,
            CONVERSATION_TARGETS_PROPERTY: ["control-app", "shared-app"],
        },
    )
    effective = effective_card_authority(caller, control)
    resolved = ResolvedCardComposition(caller_card=caller, control_card=control, effective_card=effective)
    snapshot = snapshot_from_grant(
        catalog=_catalog(),
        grant_record={
            "registry_access_id": "access-1", "client_id": "agent-1",
            "named_services": _snapshot(_catalog()).named_services,
        },
        credential=CredentialEnvelope(subject="integration:agent:user-1"),
        resource=RESOURCE,
        request_resource=RESOURCE,
        card_composition=resolved,
    )

    assert conversation_targets(effective.properties) == expected
    assert evaluate_managed_named_service(snapshot, namespace="records", operation="object.search").conversation_targets == expected


def test_malformed_conversation_target_property_grants_nothing() -> None:
    assert conversation_targets({CONVERSATION_TARGETS_PROPERTY: ["valid-app", "*"]}) == ()
    assert conversation_targets({CONVERSATION_TARGETS_PROPERTY: "valid-app"}) == ()


def test_native_admission_carries_only_resolved_targets() -> None:
    evaluation = evaluate_resolved_hub_state(
        selector={"client_id": "agent-1", "source": "native"},
        state={
            "granted": True,
            "resource": RESOURCE,
            "resource_claims": ["records:read", "records:read:any_user"],
            "conversation_targets": ["allowed-app"],
        },
    )
    assert evaluation.allowed
    assert evaluation.claims == ("records:read", "records:read:any_user")
    assert evaluation.conversation_targets == ("allowed-app",)


def test_managed_admission_carries_only_effective_card_claims() -> None:
    composition = _control_composition()
    caller = dataclasses.replace(
        composition.caller_card,
        resource_grants={
            RESOURCE: ("records:read", "records:read:any_user")
        },
    )
    effective = effective_card_authority(caller, composition.control_card)
    resolved = ResolvedCardComposition(
        caller_card=caller,
        control_card=composition.control_card,
        effective_card=effective,
    )
    snapshot = snapshot_from_grant(
        catalog=_catalog(),
        grant_record={
            "registry_access_id": "access-1",
            "client_id": "agent-1",
            "named_services": _snapshot(_catalog()).named_services,
            "resource_grants": {
                RESOURCE: ["records:read", "records:read:any_user"]
            },
        },
        credential=CredentialEnvelope(subject="integration:agent:user-1"),
        resource=RESOURCE,
        request_resource=RESOURCE,
        card_composition=resolved,
    )

    evaluation = evaluate_managed_named_service(
        snapshot,
        namespace="records",
        operation="object.search",
    )

    assert evaluation.allowed
    assert evaluation.claims == ("records:read",)


def test_named_service_denial_names_the_control_card_that_removed_it() -> None:
    composition = _control_composition()
    snapshot = dataclasses.replace(
        _snapshot(_catalog()),
        named_services={},
        card_composition=composition,
    )

    denied = evaluate_managed_named_service(
        snapshot,
        namespace="records",
        operation="object.search",
    )

    assert not denied.allowed
    assert denied.denial["ret"]["blocking_card"]["role"] == "control"
    assert denied.denial["ret"]["blocking_card"]["access_id"] == "control-card-1"
    assert denied.denial["ret"]["recovery"]["edit_route_available"] is False


def test_bearer_relay_must_match_the_authenticated_card_binding() -> None:
    snapshot = _snapshot(_catalog())
    selector = snapshot.selector()
    binding = delegated_card_binding(snapshot)

    validate_relay_selector(
        selector,
        actor={
            "user_id": "user-1",
            "identity_authority": {"delegated_card_binding": binding},
        },
    )
    with pytest.raises(NamedServiceAdmissionResolutionError):
        validate_relay_selector(
            selector,
            actor={
                "user_id": "user-1",
                "identity_authority": {
                    "delegated_card_binding": {
                        **binding,
                        "schema": DELEGATED_CARD_BINDING_SCHEMA,
                        "access_id": "other",
                    }
                },
            },
        )

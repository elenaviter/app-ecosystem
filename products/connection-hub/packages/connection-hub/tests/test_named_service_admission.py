from __future__ import annotations

import pytest

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.catalog.authorization import (
    ActiveCatalogCapabilities,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.named_service_admission import (
    DELEGATED_CARD_BINDING_SCHEMA,
    NamedServiceAdmissionResolutionError,
    delegated_card_binding,
    evaluate_managed_named_service,
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
    assert not removed.allowed
    assert removed.denial["error"]["code"] == (
        "delegated_capability_no_longer_available"
    )


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

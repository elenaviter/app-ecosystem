# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The portable card read model Gateway and Projection consume."""

from __future__ import annotations

from datetime import datetime, timezone

from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.read_model import (
    CALLER_KIND_MANUAL,
    CALLER_KIND_OAUTH,
    CALLER_KIND_RESIDENT,
    OFFER_ALREADY_ON_CARD,
    OFFER_COMPATIBLE,
    OFFER_IDENTITY_SCOPE_INCOMPATIBLE,
    build_card_view,
    compatible_resource_offers,
)
from connection_hub.delegated_credentials.catalog.descriptors import row_acceptance
from connection_hub.delegated_credentials.catalog.drift import card_resource_states
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)

MEMORIES = "https://host/api/mcp/memories*"
TASKS = "https://host/api/mcp/tasks*"
MAIL = "https://host/api/mcp/mail*"
NOW = 1_780_000_000
CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {"grant": g, "label": g, "delegable_roles": ["kdcube:role:registered"]}
                for g in ("memories:read", "tasks:use", "mail:read")
            ],
            "resources": [
                {"resource": MEMORIES, "label": "Memories", "grants": ["memories:read"],
                 "tools": {"search": {"grants": ["memories:read"]}}},
                {"resource": TASKS, "label": "Tasks", "grants": ["tasks:use"],
                 "tools": {"search": {"grants": ["tasks:use"]}, "delete": {"grants": ["tasks:use"]}}},
                {"resource": MAIL, "label": "Mail", "grants": ["mail:read"], "identity_scope": "family",
                 "tools": {"read": {"grants": ["mail:read"]}}},
            ],
        }
    }
}


class _Policy:
    def __init__(self, resource, operation, mode, remaining=None):
        self._payload = {
            "policy_id": f"pol-{operation}",
            "authority": {"access_id": "agent-x", "resource": resource, "surface": "outer", "operation": operation},
            "mode": mode,
            "revision": 1,
            "state": "available",
            "remaining": remaining,
        }

    def to_public_dict(self):
        return dict(self._payload)


def test_view_carries_profile_resources_operations_states_and_policies():
    document = CatalogDocument.build(CONNECTIONS, created_at=datetime.fromtimestamp(NOW, tz=timezone.utc))
    config = oauth_delegated_config_from_connections(document.connections)
    authority = CardAuthority(
        access_id="agent-x",
        client_id="kdcube-agent:workspace@1-0:lg-react",
        grantor_subject="user-1",
        delegate_subject="integration:kdcube-agent:workspace@1-0:lg-react:user-1",
        source="agent",
        label="lg-react",
        card_revision=4,
        catalog_version=document.version,
        resource_grants={MEMORIES: ("memories:read",), TASKS: ("tasks:use",)},
        resource_operations={MEMORIES: ("search",), TASKS: ("search", "delete")},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        created_at=NOW,
        expires_at=NOW + 3600,
        resource_acceptance={
            MEMORIES: row_acceptance(config.card_selector_config(MEMORIES), catalog_version=document.version),
            TASKS: row_acceptance(config.card_selector_config(TASKS), catalog_version=document.version),
        },
        provenance={"migrated_from": [{"access_id": "agent-old"}]},
    )
    view = build_card_view(
        authority,
        resource_states=card_resource_states(card=authority, active=document, active_config=config),
        row_for=config.card_selector_config,
        policies=[_Policy(TASKS, "delete", "once", remaining=1)],
    )
    assert view.caller_kind == CALLER_KIND_RESIDENT
    assert view.profile is not None and view.profile.agent_id == "lg-react"
    assert view.profile.access_id != view.access_id  # legacy id in this fixture
    assert [entry.resource for entry in view.resources] == [MEMORIES, TASKS]
    tasks = view.resource(TASKS)
    assert tasks.kind == "catalog" and tasks.state == "current" and tasks.label == "Tasks"
    # Operations are normalized in sorted order.
    assert [op.name for op in tasks.operations] == ["delete", "search"]
    # Equal names stay qualified: the policy binds to tasks:delete only.
    assert tasks.operations[0].policy["mode"] == "once"
    assert tasks.operations[1].policy is None
    assert view.resource(MEMORIES).operations[0].policy is None
    assert tasks.operations[1].accepted_digest == tasks.operations[1].current_digest != ""
    payload = view.to_dict()
    assert payload["profile"]["application"] == "workspace@1-0"
    assert payload["resources"][1]["operations"][0]["policy"]["mode"] == "once"
    assert payload["provenance"] == {"migrated_from": [{"access_id": "agent-old"}]}
    # Nothing secret is in the view.
    assert "access_token" not in str(payload) and "refresh_token" not in str(payload)
    assert type(view).from_dict(payload) == view


def test_caller_kind_follows_the_client_family():
    def _authority(client_id, source):
        return CardAuthority(
            access_id="x", client_id=client_id, grantor_subject="u", delegate_subject="d",
            source=source, resource_grants={MEMORIES: ("memories:read",)},
            resource_operations={MEMORIES: ("search",)},
            named_service_operations=NamedServiceSelection.none(), expires_at=NOW + 10,
        )

    assert build_card_view(_authority("dcr-1", "oauth")).caller_kind == CALLER_KIND_OAUTH
    assert build_card_view(_authority("automation:abc", "manual")).caller_kind == CALLER_KIND_MANUAL
    resident = build_card_view(_authority("kdcube-agent:app@1-0:agent", "agent"))
    assert resident.caller_kind == CALLER_KIND_RESIDENT
    # Without current facts every resource reads unknown, never current.
    assert resident.resources[0].state == "unknown"
    assert resident.resources[0].operations[0].state == "unknown"


def test_compatible_offers_explain_every_excluded_resource():
    options = [
        {"resource": MEMORIES, "label": "Memories", "identity_scope": "grantor"},
        {"resource": TASKS, "label": "Tasks", "identity_scope": "grantor"},
        {"resource": MAIL, "label": "Mail", "identity_scope": "grantor_identity_family"},
        {"resource": "*", "label": "Everything", "identity_scope": "grantor", "admin_only": True},
    ]
    offers = compatible_resource_offers(
        card_resources=[MEMORIES], card_identity_scope="grantor", options=options
    )
    by_resource = {item["resource"]: item for item in offers}
    assert by_resource[MEMORIES]["reason"] == OFFER_ALREADY_ON_CARD
    assert by_resource[TASKS]["reason"] == OFFER_COMPATIBLE and by_resource[TASKS]["compatible"]
    assert by_resource[MAIL]["reason"] == OFFER_IDENTITY_SCOPE_INCOMPATIBLE
    assert by_resource[MAIL]["card_identity_scope"] == "grantor"
    assert by_resource["*"]["reason"] == "admin_only"
    admin = compatible_resource_offers(
        card_resources=[MEMORIES], card_identity_scope="grantor", options=options, platform_admin=True
    )
    assert {item["resource"]: item["compatible"] for item in admin}["*"] is True

from __future__ import annotations

from dataclasses import replace

import pytest
from connection_hub.delegated_credentials.cards.identity import ResidentCallerProfile
from connection_hub.delegated_credentials.cards.read_model import (
    CALLER_KIND_RESIDENT,
    CardOperationView,
    CardResourceView,
)
from connection_hub.delegated_credentials.cards.read_model import (
    DelegatedCardView as CardReadView,
)
from connection_hub.delegated_gateway import (
    DISCOVER_REQUESTABLE,
    GatewayContractError,
    GatewayResourceMetadata,
    RecoveryLink,
    adapt_card_view,
)
from connection_hub.delegated_gateway.models import RESOURCE_ACTIVE, RESOURCE_DISABLED


def _card_resource(*, state: str = "changed") -> CardResourceView:
    return CardResourceView(
        resource="urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567",
        kind="remote_mcp",
        provider="remote_mcp",
        label="Records",
        state=state,
        identity_scope="grantor",
        grants=("external_mcp:use",),
        operations=(
            CardOperationView(
                name="records.read",
                state="current",
                accepted_digest="a" * 64,
                current_digest="a" * 64,
                policy={
                    "authority": {
                        "access_id": "agent-access-1",
                        "resource": (
                            "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"
                        ),
                        "surface": "outer",
                        "operation": "records.read",
                    },
                    "mode": "once",
                    "revision": 3,
                    "state": "available",
                    "remaining": 1,
                },
            ),
            CardOperationView(
                name="records.delete",
                state="changed",
                accepted_digest="b" * 64,
                current_digest="c" * 64,
            ),
        ),
        accepted_revision="2",
        current_revision="3",
        accepted_digest="d" * 64,
        current_digest="e" * 64,
    )


def _card(*resources: CardResourceView) -> CardReadView:
    profile = ResidentCallerProfile(
        grantor_subject="owner-secret-subject",
        application="workspace@1-0",
        agent_id="researcher",
    )
    return CardReadView(
        access_id=profile.access_id,
        client_id=profile.client_id,
        caller_kind=CALLER_KIND_RESIDENT,
        profile=profile,
        grantor_subject=profile.grantor_subject,
        delegate_subject="integration:resident:researcher",
        source="agent",
        label="Researcher",
        card_revision=7,
        catalog_version="catalog-3",
        state="active",
        created_at=1,
        expires_at=2_000_000_000,
        identity_scope="grantor",
        resources=resources,
    )


def _metadata(_resource: CardResourceView) -> GatewayResourceMetadata:
    return GatewayResourceMetadata(
        endpoint_relation="delegated_mcp_gateway",
        recovery=(
            RecoveryLink(
                code="operation_descriptor_changed",
                href="/connections/access/agent-access-1",
            ),
        ),
        provider_metadata={"locator": "remote-mcp"},
    )


def test_card_read_model_adapts_without_reproducing_identity_or_drift_logic():
    source = _card(_card_resource())

    gateway = adapt_card_view(
        source,
        metadata_for=_metadata,
        capabilities=(DISCOVER_REQUESTABLE,),
    )

    assert gateway.access_id == source.access_id
    assert gateway.caller_profile_id == source.profile.client_id
    assert gateway.grantor_subject == "owner-secret-subject"
    assert gateway.capabilities == (DISCOVER_REQUESTABLE,)
    resource = gateway.resources[0]
    assert resource.state == RESOURCE_ACTIVE
    assert resource.provider_id == "remote_mcp"
    assert resource.operations == ("records.delete", "records.read")
    assert resource.invocation_policies["records.read"].mode == "once"
    assert resource.invocation_policies["records.read"].remaining == 1
    assert resource.accepted_descriptor.operation_digests == {
        "records.delete": "b" * 64,
        "records.read": "a" * 64,
    }


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [("removed", "descriptor_missing"), ("unknown", "descriptor_unknown")],
)
def test_removed_and_unknown_card_resources_adapt_disabled(
    state: str, expected_reason: str
):
    gateway = adapt_card_view(
        _card(_card_resource(state=state)), metadata_for=_metadata
    )

    assert gateway.resources[0].state == RESOURCE_DISABLED
    assert gateway.resources[0].unavailable_reason == expected_reason


def test_card_adapter_rejects_policy_bound_to_another_operation():
    resource = _card_resource()
    operation = resource.operations[0]
    mismatched = replace(
        operation,
        policy={
            **operation.policy,
            "authority": {**operation.policy["authority"], "operation": "other"},
        },
    )
    resource = replace(resource, operations=(mismatched, *resource.operations[1:]))

    with pytest.raises(
        GatewayContractError, match="card_invocation_policy_authority_mismatch"
    ):
        adapt_card_view(_card(resource), metadata_for=_metadata)

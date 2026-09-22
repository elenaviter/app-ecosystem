from __future__ import annotations

from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AUTOMATION,
    CARD_KIND_CONNECTOR,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.live_grant import (
    live_grants_for_resource,
    whole_card_grants,
)


def _card(card_kind: str) -> CardAuthority:
    return CardAuthority(
        access_id="aut_0123456789abcdef",
        client_id="codex",
        grantor_subject="user-1",
        delegate_subject="integration:codex:user-1",
        source="oauth",
        card_kind=card_kind,
        card_revision=1,
        operations=("work.read", "message.send"),
        resource_grants={
            "https://hub.example.test/mcp/problem_board": ("work:read",),
            "urn:kdcube:application-api": ("application_api:invoke",),
        },
        resource_operations={},
        named_service_operations=NamedServiceSelection.unknown(),
        expires_at=2_000_000_000,
    )


def test_automation_whole_card_refresh_collects_every_resource_grant() -> None:
    assert whole_card_grants(_card(CARD_KIND_AUTOMATION)) == (
        "work:read",
        "application_api:invoke",
    )


def test_connector_refresh_remains_bound_to_one_resource() -> None:
    card = _card(CARD_KIND_CONNECTOR)

    assert live_grants_for_resource(card, "*") is None
    assert live_grants_for_resource(
        card,
        "https://hub.example.test/mcp/problem_board",
    ) == ("work:read",)


def test_per_surface_lookup_rejects_an_empty_resource() -> None:
    assert live_grants_for_resource(_card(CARD_KIND_AUTOMATION), "") is None


def test_connector_has_no_whole_card_union() -> None:
    assert whole_card_grants(_card(CARD_KIND_CONNECTOR)) is None

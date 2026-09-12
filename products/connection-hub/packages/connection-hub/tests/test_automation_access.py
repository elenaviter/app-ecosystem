from __future__ import annotations

from types import SimpleNamespace

from connection_hub.delegated_credentials.automation_access import (
    ACCESS_SOURCE_AGENT,
    ACCESS_SOURCE_MANUAL,
    ACCESS_SOURCE_OAUTH,
    AutomationAccessRecord,
    AutomationAccessService,
    _account_scope_claims_for_requirements,
)
from connection_hub.delegated_credentials.oauth.clients import (
    client_uses_full_card_catalog,
)


def _public_record(*, source: str, metadata=None) -> dict:
    return AutomationAccessRecord(
        access_id="access-1",
        label="Caller",
        client_id="client-1",
        grantor_subject="owner-1",
        delegate_subject="delegate-1",
        operations=(),
        resource_grants={"https://example.test/mcp/service": ("work:read",)},
        source=source,
        client_metadata=metadata or {},
    ).to_public_dict()


def test_public_card_separates_credential_delivery_from_resource_reach() -> None:
    hosted = _public_record(source=ACCESS_SOURCE_AGENT)
    issued = _public_record(source=ACCESS_SOURCE_MANUAL)
    app = _public_record(source=ACCESS_SOURCE_OAUTH)
    connected_client = _public_record(
        source=ACCESS_SOURCE_OAUTH,
        metadata={"kdcube_credential_use": "multi_resource"},
    )

    assert (hosted["credential_delivery"], hosted["credential_reach"]) == (
        "hosted",
        "multi_resource",
    )
    assert (issued["credential_delivery"], issued["credential_reach"]) == (
        "issued_token",
        "multi_resource",
    )
    assert (app["credential_delivery"], app["credential_reach"]) == (
        "oauth",
        "single_resource",
    )
    assert (
        connected_client["credential_delivery"],
        connected_client["credential_reach"],
    ) == ("oauth", "multi_resource")


def test_multi_resource_hint_reads_asserted_client_metadata_snapshot() -> None:
    assert client_uses_full_card_catalog(
        {"client_metadata": {"kdcube_credential_use": "multi_resource"}}
    )
    assert not client_uses_full_card_catalog(
        {"client_metadata": {"kdcube_credential_use": "single_resource"}}
    )


def test_oauth_catalog_reach_preserves_entry_bound_clients_and_full_clients() -> None:
    class Probe:
        def _reachable_through_door(self, entry_resource, *, config):
            assert config == "catalog"
            return {f"{entry_resource}/child"}

    service = Probe()
    entry = "https://example.test/mcp/entry"

    assert AutomationAccessService._oauth_allowed_resources(
        service,
        entry_resource=entry,
        client_metadata={},
        config="catalog",
    ) == {entry, f"{entry}/child"}
    assert AutomationAccessService._oauth_allowed_resources(
        service,
        entry_resource=entry,
        client_metadata={"kdcube_credential_use": "multi_resource"},
        config="catalog",
    ) is None


def test_explicit_connected_account_claims_are_effective_for_admission() -> None:
    record = SimpleNamespace(
        account_scope={
            "slack": {
                "workspace-1": ("slack:channels", "slack:post"),
            }
        }
    )

    held = _account_scope_claims_for_requirements(
        record,
        required={"named_services:use", "slack:post"},
    )

    assert held == {"slack:channels", "slack:post"}


def test_account_wildcard_expands_only_current_provider_requirements() -> None:
    record = SimpleNamespace(
        account_scope={
            "slack": {"workspace-1": ("*",)},
            "google": {"account-1": ("gmail:read",)},
        }
    )

    held = _account_scope_claims_for_requirements(
        record,
        required={
            "named_services:use",
            "slack:post",
            "gmail:read",
        },
    )

    assert held == {"slack:post", "gmail:read"}


class _Operation:
    def __init__(self, name: str) -> None:
        self.name = name


class _Catalog:
    """Offers a fixed operation list per resource.

    Grant filtering is exercised elsewhere; these assertions are about what an
    absent or drifted selection resolves to.
    """

    def __init__(self, by_resource: dict[str, tuple[str, ...]]) -> None:
        self._by_resource = by_resource

    def tools_for_scopes(self, grants, *, resource=None):
        del grants
        return [_Operation(name) for name in self._by_resource.get(resource, ())]


_RESOURCE = "https://host/api/integrations/bundles/t/p/lab@1-0/public/mcp/lab"
_GRANTS = {_RESOURCE: ["lab:read"]}
_CATALOG = _Catalog(
    {
        _RESOURCE: (
            "about_module_one",
            "analyze_binary_contract",
            "simulate_binary_contract",
        )
    }
)


def _resolve(**kwargs):
    return AutomationAccessService._resolve_resource_operations(
        None,
        resource_grants=_GRANTS,
        config=_CATALOG,
        **kwargs,
    )


def test_absent_selection_selects_nothing() -> None:
    """An edit that names no operation must not inherit the whole catalog.

    Before the resource-qualified rewrite an absent selection resolved to every
    operation the grants allowed, so a drift-pruned card gained the survivors on
    save. This is the assertion that fails on that behaviour.
    """
    resolved = _resolve(resource_operations=None)

    assert resolved == {_RESOURCE: []}


def test_default_all_is_opt_in() -> None:
    """Only an explicit default_all may expand an absent selection."""
    resolved = _resolve(resource_operations=None, default_all=True)

    assert resolved == {
        _RESOURCE: [
            "about_module_one",
            "analyze_binary_contract",
            "simulate_binary_contract",
        ]
    }


def test_pruning_a_drifted_selection_does_not_widen_the_card() -> None:
    """The card held one operation the catalog no longer offers.

    Pruning leaves nothing selected rather than falling back to the catalog.
    Covers the explicit-selection branch, where the absent-selection assertion
    above does not reach.
    """
    catalog = _Catalog(
        {_RESOURCE: ("analyze_binary_contract", "simulate_binary_contract")}
    )

    resolved = AutomationAccessService._resolve_resource_operations(
        None,
        resource_grants=_GRANTS,
        resource_operations={_RESOURCE: ["about_module_one"]},
        prune_unknown=True,
        config=catalog,
    )

    assert resolved == {_RESOURCE: []}


def _catalog_with_a_declared_door():
    from connection_hub.delegated_credentials.oauth.config import (
        oauth_delegated_config_from_connections,
    )

    return oauth_delegated_config_from_connections(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [
                        {"grant": "work:relay", "label": "Relay"},
                        {"grant": "work:coordinate", "label": "Coordinate"},
                    ],
                    "resources": [
                        {
                            "resource": "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*",
                            "label": "Problem Board",
                            "grants": ["work:relay"],
                        },
                        {"resource": "*", "label": "Everything", "grants": ["work:coordinate"]},
                    ],
                }
            }
        }
    )


def _resolver():
    return AutomationAccessService._declared_resource_keys


def test_issuance_persists_the_declared_door_not_the_host_that_served_consent() -> None:
    """An OAuth resource indicator is concrete; the door it names is not.

    A client must ask for a concrete URL, so it asks for the hostname that
    happens to be serving it today. Writing that URL onto the card pins the card
    to that hostname, and the same service reached through any other name stops
    matching. The catalog then grows a second row for a door it already had.
    """

    config = _catalog_with_a_declared_door()
    declared = "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*"
    requested = (
        "https://some-tunnel.example.test/api/integrations/bundles"
        "/demo-tenant/demo-project/problem-board@1-0/public/mcp/problem_board"
    )

    resolved, rewritten = _resolver()(
        AutomationAccessService, config, {requested: ["work:relay"]}
    )

    assert list(resolved) == [declared]
    assert resolved[declared] == ["work:relay"]
    assert rewritten == {requested: declared}


def test_two_hostnames_for_one_door_are_one_grant() -> None:
    config = _catalog_with_a_declared_door()
    declared = "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*"
    base = "/api/integrations/bundles/demo-tenant/demo-project/problem-board@1-0/public/mcp/problem_board"

    resolved, _ = _resolver()(
        AutomationAccessService,
        config,
        {
            f"https://one.example.test{base}": ["work:relay"],
            f"https://two.example.test{base}": ["work:coordinate"],
        },
    )

    assert list(resolved) == [declared]
    assert resolved[declared] == ["work:relay", "work:coordinate"]


def test_a_resource_no_declaration_covers_keeps_its_literal() -> None:
    """The one case that genuinely has nothing to point at."""

    config = _catalog_with_a_declared_door()
    stranger = "https://elsewhere.example.test/mcp/something-else"

    resolved, rewritten = _resolver()(
        AutomationAccessService, config, {stranger: ["work:relay"]}
    )

    assert list(resolved) == [stranger]
    assert rewritten == {}


def test_the_all_resource_row_never_swallows_a_concrete_request() -> None:
    """It is a declared admin surface, so collapsing into it would widen the card."""

    config = _catalog_with_a_declared_door()
    stranger = "https://elsewhere.example.test/mcp/something-else"

    resolved, _ = _resolver()(
        AutomationAccessService, config, {stranger: ["work:relay"]}
    )

    assert "*" not in resolved

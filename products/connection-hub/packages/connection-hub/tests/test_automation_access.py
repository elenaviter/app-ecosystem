from __future__ import annotations

from types import SimpleNamespace

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    _account_scope_claims_for_requirements,
)


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

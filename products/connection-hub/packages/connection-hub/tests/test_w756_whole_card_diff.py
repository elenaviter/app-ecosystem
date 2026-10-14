"""Whole-Save preflight unit evidence, not productive PB/PG qualification."""
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.oauth.config import (
    OAuthDelegatedResourceConfig, OAuthDelegatedToolConfig,
)
from connection_hub.delegated_credentials.whole_card_save import (
    WholeCardSaveRefused, check_card_pin, check_catalog_pin, normalize_selection, added_invocation_modes,
    same_selection,
)
from connection_hub.delegated_credentials.cards.model import NamedServiceSelection


class Catalog:
    def card_selector_config(self, resource):
        return OAuthDelegatedResourceConfig(resource=resource, grants=("read",), tools=tuple(
            OAuthDelegatedToolConfig(name=name, label=name, grants=("read",))
            for name in ("read", "write", "delete")))

    def tools_for_scopes(self, grants, *, resource):
        return self.card_selector_config(resource).tools if "read" in grants else ()


def original(**fields):
    values = dict(card_revision=29, resource_grants={"service": ("read",)},
                  resource_operations={"service": ("read",)}, properties={"kept": "same"})
    values.update(fields)
    return SimpleNamespace(**values)


def test_stale_card_is_refused_before_a_catalog_is_even_read():
    with pytest.raises(WholeCardSaveRefused, match="managed_card_edit_revision_changed"):
        check_card_pin(original(), 28)


def test_moved_catalog_has_exact_expected_and_active_fields():
    with pytest.raises(WholeCardSaveRefused) as failed:
        check_catalog_pin("rendered-29", "active-30")
    assert failed.value.to_dict() == {
        "ok": False, "error": "consent_catalog_changed", "status": 409,
        "message": "The catalog changed. Reload it before saving; your draft is kept.",
        "expected_catalog_version": "rendered-29", "active_catalog_version": "active-30",
    }


def test_all_is_expanded_to_concrete_current_operations_without_mutating_the_draft():
    selection = {"resource_grants": {"service": ["read"]}, "resource_operations": {"service": ["*"]}}
    result = normalize_selection(original(), selection, config=Catalog())
    assert result["resource_operations"] == {"service": ["delete", "read", "write"]}
    assert selection["resource_operations"]["service"] == ["*"]


def test_no_wildcard_can_be_retained_as_a_policy_operation():
    modes = added_invocation_modes(original=original(), candidate=original(
        resource_operations={"service": ("read", "write", "delete")}), config=Catalog(),
        modes={"service": {"delete": "once", "write": "always"}}, policy_bearing=True)
    assert modes == {"service": {"delete": "once", "write": "always"}}


def test_missing_one_of_n_additions_is_one_named_refusal():
    with pytest.raises(WholeCardSaveRefused, match="invocation_mode_required") as failed:
        added_invocation_modes(original=original(), candidate=original(
            resource_operations={"service": ("read", "write", "delete")}), config=Catalog(),
            modes={"service": {"write": "always"}}, policy_bearing=True)
    assert failed.value.to_dict()["missing_invocation_modes"] == {"service": ["delete"]}


def test_held_operation_cannot_reset_its_policy_during_save():
    with pytest.raises(WholeCardSaveRefused, match="invocation_modes_invalid"):
        added_invocation_modes(original=original(), candidate=original(), config=Catalog(),
            modes={"service": {"read": "once"}}, policy_bearing=True)


def test_controls_require_no_modes_and_refuse_a_mode_carrier():
    candidate = original(resource_operations={"service": ("read", "write")})
    assert added_invocation_modes(original=original(), candidate=candidate, config=Catalog(),
                                  modes=None, policy_bearing=False) == {}
    with pytest.raises(WholeCardSaveRefused, match="invocation_modes_invalid"):
        added_invocation_modes(original=original(), candidate=candidate, config=Catalog(),
                               modes={"service": {"write": "once"}}, policy_bearing=False)


def test_application_api_operations_are_not_invocation_policy_effects():
    assert added_invocation_modes(original=original(resource_operations={}),
        candidate=original(resource_operations={"*": ("project.plan.read",)}), config=Catalog(),
        modes={}, policy_bearing=True) == {}


NAMED = {"namespaces": {"Svc:": {"tools": {
    "reader": {"operation": "inner-read", "grants": ["read"]},
    "writer": {"operation": "inner-write", "grants": ["write"]},
}}}}


class NamedCatalog(Catalog):
    def card_selector_config(self, resource):
        return OAuthDelegatedResourceConfig(resource=resource, grants=("read", "write"),
                                            named_services=NAMED)


def test_named_all_is_concrete_grant_filtered_and_uses_existing_managed_wire():
    from connection_hub.delegated_credentials.managed_card_edit_forward import managed_card_edit_body
    selection = {"named_service_operations": "*"}
    normalized = normalize_selection(original(), selection, config=NamedCatalog())
    assert normalized == {"named_service_operations": {"service": {"svc": ["inner-read"]}}}
    assert selection == {"named_service_operations": "*"}
    body = managed_card_edit_body(actor_subject="person", project_ref="work:project:fixture",
        request_id="hub-owned", kind="person_my", principal_key="user:person", original_revision=29,
        selection=normalized, expected_catalog_version="catalog-29")
    assert body["selection"] == normalized


def test_named_namespace_all_is_expanded_under_the_same_catalog():
    normalized = normalize_selection(original(), {"named_service_operations": {
        "service": {"Svc:": ["*"]}}}, config=NamedCatalog())
    assert normalized == {"named_service_operations": {"service": {"svc": ["inner-read"]}}}


def test_named_all_same_as_stored_boundary_is_noop_but_new_catalog_names_are_a_change():
    before = original(named_service_operations=NamedServiceSelection.all(), named_services=NAMED)
    normalized = normalize_selection(before, {"named_service_operations": "*"}, config=NamedCatalog())
    assert same_selection(before, normalized)
    before.named_services = {"namespaces": {}}
    assert not same_selection(before, normalized)


def test_unknown_namespace_all_refuses_instead_of_retaining_a_wildcard():
    with pytest.raises(WholeCardSaveRefused, match="card_selection_wildcard_unavailable"):
        normalize_selection(original(), {"named_service_operations": {
            "service": {"missing": ["*"]}}}, config=NamedCatalog())

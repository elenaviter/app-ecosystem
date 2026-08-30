from __future__ import annotations

from types import SimpleNamespace

from connection_hub.delegated_credentials.automation_access import (
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

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from kdcube_ai_app.apps.chat.proc.rest.management.secret_contracts import (
    SECRET_OPERATIONS,
    SECRET_RESOURCE_SELECTOR,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _entrypoint_defaults() -> Mapping[str, Any]:
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    entrypoint = module.ConnectionHubEntrypoint.__new__(
        module.ConnectionHubEntrypoint
    )
    return entrypoint.configuration_defaults()


def _template_config() -> Mapping[str, Any]:
    bundle_root = Path(__file__).resolve().parents[1]
    document = yaml.safe_load(
        (bundle_root / "config" / "bundles.template.yaml").read_text(
            encoding="utf-8"
        )
    )
    item = next(
        row
        for row in document["bundles"]["items"]
        if row["id"] == "connection-hub@1-0"
    )
    return item["config"]


def _assert_secret_management_contract(config: Mapping[str, Any]) -> None:
    delegated = config["connections"]["delegated_credentials"]
    oauth = delegated["oauth"]
    admission = delegated["admission"]["services"]["kdcube-management"]

    capabilities = {row["grant"]: row for row in oauth["capabilities"]}
    for operation in SECRET_OPERATIONS:
        assert capabilities[operation]["delegable_roles"] == [
            "kdcube:role:super-admin"
        ]

    secret_resource = next(
        row
        for row in oauth["resources"]
        if row["resource"] == SECRET_RESOURCE_SELECTOR
    )
    assert secret_resource["admin_only"] is True
    assert secret_resource["resource_selection"] is True
    assert set(secret_resource["operations"]) == set(SECRET_OPERATIONS)
    for operation in SECRET_OPERATIONS:
        assert secret_resource["operations"][operation]["grants"] == [operation]

    assert SECRET_RESOURCE_SELECTOR in admission["resources"]
    assert set(SECRET_OPERATIONS).issubset(
        admission["request_bound_operations"]
    )


def test_entrypoint_and_install_template_expose_same_secret_management_contract():
    _assert_secret_management_contract(_entrypoint_defaults())
    _assert_secret_management_contract(_template_config())

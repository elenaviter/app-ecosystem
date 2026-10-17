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
    assert not set(SECRET_OPERATIONS).intersection(
        admission["request_bound_operations"]
    )


def test_entrypoint_and_install_template_expose_same_secret_management_contract():
    _assert_secret_management_contract(_entrypoint_defaults())
    _assert_secret_management_contract(_template_config())


# W890: kdcube's delegated user props import (kdcube_ai_app/apps/chat/proc/rest/management/user_props_contracts.py).
# Literal names, so this test does not depend on which kdcube revision is on the path.
USER_PROPS_IMPORT_OPERATION = "kdcube.management.user_props.import"
USER_PROPS_RESOURCE_SELECTOR = "urn:kdcube:management:user-props:*:*"


def _assert_user_props_import_contract(config: Mapping[str, Any]) -> None:
    delegated = config["connections"]["delegated_credentials"]
    capabilities = {row["grant"]: row for row in delegated["oauth"]["capabilities"]}
    assert capabilities[USER_PROPS_IMPORT_OPERATION]["delegable_roles"] == ["kdcube:role:super-admin"]
    resource = next(row for row in delegated["oauth"]["resources"]
                    if row["resource"] == USER_PROPS_RESOURCE_SELECTOR)
    assert resource["admin_only"] is True
    assert set(resource["operations"]) == {USER_PROPS_IMPORT_OPERATION}
    assert resource["operations"][USER_PROPS_IMPORT_OPERATION]["grants"] == [USER_PROPS_IMPORT_OPERATION]
    admission = delegated["admission"]["services"]["kdcube-management"]
    assert USER_PROPS_RESOURCE_SELECTOR in admission["resources"]
    assert USER_PROPS_IMPORT_OPERATION in admission["request_bound_operations"], "Once approves one exact batch"


def test_entrypoint_and_template_both_declare_the_user_props_import() -> None:
    _assert_user_props_import_contract(_entrypoint_defaults())
    _assert_user_props_import_contract(_template_config())

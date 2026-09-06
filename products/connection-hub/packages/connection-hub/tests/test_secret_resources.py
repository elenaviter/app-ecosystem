from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.delegated_credentials.secret_resources import (
    SecretResource,
    SecretResourceError,
    secret_resource,
    validate_secret_card_resource,
    whole_deployment_secret_resources,
)


def test_exact_prefix_and_whole_deployment_selectors_are_typed():
    exact = secret_resource(
        tenant="tenant-a",
        project="project-a",
        scope="platform",
        scope_id="_",
        key="platform.services.brave.api_key",
    )
    prefix = secret_resource(
        tenant="tenant-a",
        project="project-a",
        scope="platform",
        scope_id="_",
        key="platform.services.*",
    )
    whole = whole_deployment_secret_resources(
        tenant="tenant-a",
        project="project-a",
    )

    assert SecretResource.parse(exact).broad is False
    assert SecretResource.parse(prefix).broad is True
    assert [SecretResource.parse(value).scope for value in whole] == [
        "platform",
        "bundle",
        "user",
    ]
    assert all(SecretResource.parse(value).broad for value in whole)


@pytest.mark.parametrize(
    "resource",
    [
        "urn:kdcube:management:secret:*:*:platform:_:platform.*",
        "urn:kdcube:management:secret:tenant-a:project-a:platform:_:services.*",
        "urn:kdcube:management:secret:tenant-a:project-a:platform:_:platform.*.token",
        "urn:kdcube:management:secret:tenant-a:project-a:bundle:*:platform.*",
        "urn:kdcube:management:secret:tenant-a:project-a:user:user-1~*:token",
    ],
)
def test_free_form_or_cross_scope_wildcards_are_rejected(resource: str):
    with pytest.raises(SecretResourceError):
        SecretResource.parse(resource)


def test_card_resource_must_name_the_current_deployment():
    resource = secret_resource(
        tenant="tenant-a",
        project="project-a",
        scope="user",
        scope_id="user-1",
        key="provider.*",
    )
    assert validate_secret_card_resource(
        resource,
        tenant="tenant-a",
        project="project-a",
    ) is not None
    with pytest.raises(SecretResourceError, match="deployment_mismatch"):
        validate_secret_card_resource(
            resource,
            tenant="tenant-b",
            project="project-a",
        )


@pytest.mark.asyncio
async def test_admin_resource_option_carries_typed_current_deployment_context():
    connections = {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {
                        "grant": "kdcube.management.secret.value.write",
                        "label": "Write secrets",
                        "delegable_roles": ["kdcube:role:super-admin"],
                    }
                ],
                "resources": [
                    {
                        "resource": "urn:kdcube:management:secret:*:*:*:*:*",
                        "label": "KDCube secret management",
                        "admin_only": True,
                        "resource_selection": True,
                        "selector_type": "kdcube_secret",
                        "operations": {
                            "kdcube.management.secret.value.write": {
                                "grants": [
                                    "kdcube.management.secret.value.write"
                                ]
                            }
                        },
                    }
                ],
            }
        }
    }

    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=connections)

    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=oauth_delegated_config_from_connections(connections),
        catalog_resolver=_Resolver(),
    )

    options = await service.resource_options(
        {
            "user_id": "user-1",
            "roles": ["kdcube:role:super-admin"],
            "permissions": [],
        }
    )

    assert options[0]["selector_type"] == "kdcube_secret"
    assert options[0]["selector_context"] == {
        "tenant": "tenant-a",
        "project": "project-a",
    }

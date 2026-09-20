from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.application_resources import (
    ApplicationResource,
    ApplicationResourceError,
    application_resource,
    validate_application_card_resource,
)


def test_application_wildcards_match_one_component_only() -> None:
    deployment = ApplicationResource.parse(
        "urn:kdcube:app:tenant-a:project-a:*:*"
    )
    exact = ApplicationResource.parse(
        "urn:kdcube:app:tenant-a:project-a:problem-board@1-0:coordinator"
    )

    assert deployment.matches(exact)
    assert not deployment.matches(
        ApplicationResource.parse(
            "urn:kdcube:app:tenant-a:project-b:problem-board@1-0:coordinator"
        )
    )
    assert deployment.resource.count(":") == exact.resource.count(":")


def test_application_selector_intersection_preserves_both_axes() -> None:
    all_agents_for_app = ApplicationResource.parse(
        "urn:kdcube:app:tenant-a:project-a:problem-board@1-0:*"
    )
    one_agent_across_apps = ApplicationResource.parse(
        "urn:kdcube:app:tenant-a:project-a:*:coordinator"
    )

    assert all_agents_for_app.intersection(one_agent_across_apps) == (
        ApplicationResource.parse(
            "urn:kdcube:app:tenant-a:project-a:problem-board@1-0:coordinator"
        )
    )
    assert all_agents_for_app.intersection(
        ApplicationResource.parse(
            "urn:kdcube:app:tenant-a:project-b:problem-board@1-0:*"
        )
    ) is None


@pytest.mark.parametrize(
    "resource",
    [
        "urn:kdcube:app:*:project-a:*:*",
        "urn:kdcube:app:tenant-a:project-a:problem-*:agent",
        "urn:kdcube:app:tenant-a:project-a:problem-board@1-0:*:extra",
        "urn:kdcube:app:tenant-a:project-a:**:agent",
    ],
)
def test_application_resource_rejects_cross_component_patterns(resource: str) -> None:
    with pytest.raises(ApplicationResourceError):
        ApplicationResource.parse(resource)


def test_application_resource_is_canonical_and_deployment_bound() -> None:
    resource = application_resource(
        tenant="tenant-a",
        project="project-a",
        application="problem-board@1-0",
        agent="worker",
    )

    assert validate_application_card_resource(
        resource,
        tenant="tenant-a",
        project="project-a",
        exact=True,
    ) == ApplicationResource.parse(resource)
    with pytest.raises(ApplicationResourceError, match="deployment_mismatch"):
        validate_application_card_resource(
            resource,
            tenant="tenant-b",
            project="project-a",
        )

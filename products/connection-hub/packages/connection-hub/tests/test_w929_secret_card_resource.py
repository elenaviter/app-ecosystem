# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W929: a Card keeps the exact KDCube secret it is granted, on both the save and the create path.

Live 23 Oct 10:31Z: the operator approved Once for one secret on the CLI's existing Card, and the save was
refused delegated_access_unknown_resources. resolve_declared_resource rewrote the exact secret to the catalog's
kdcube_secret row pattern (urn:kdcube:management:secret:*:*:*:*:*), which is not a Card resource: it fails
validate_secret_card_resource, so the row lookup found nothing. A secret is offered one by one; the Card names it.
"""

from __future__ import annotations

import copy

import pytest

from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
from connection_hub.delegated_credentials.resource_operations import resolve_declared_resource
from test_resident_profile_cards import GRANTOR, MEMORIES, _connections, _Harness

SECRET_ROW = "urn:kdcube:management:secret:*:*:*:*:*"
SECRET = "urn:kdcube:management:secret:tenant-a:project-a:bundle:connection-hub@1-0:connections.signing_key"
WRITE = "kdcube.management.secret.value.write"
ADMIN = {"user_id": GRANTOR, "roles": ["kdcube:role:super-admin", "kdcube:role:registered"], "permissions": []}
CLIENT = "dcr-connection-hub-cli"
CONCRETE = "https://host/api/mcp/memories"


def _connections_with_secrets():
    connections = copy.deepcopy(_connections())
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["capabilities"].append(
        {"grant": WRITE, "label": "Write secrets", "delegable_roles": ["kdcube:role:super-admin"]}
    )
    oauth["resources"].append({
        "resource": SECRET_ROW,
        "label": "KDCube secret management",
        "admin_only": True,
        "resource_selection": True,
        "selector_type": "kdcube_secret",
        "operations": {WRITE: {"grants": [WRITE]}},
    })
    return connections


def test_a_secret_resolves_to_itself_and_a_door_still_resolves_to_its_pattern():
    config = oauth_delegated_config_from_connections(_connections_with_secrets())

    assert resolve_declared_resource(config, SECRET) == (SECRET, True)
    assert resolve_declared_resource(config, CONCRETE) == (MEMORIES, False)


@pytest.mark.asyncio
async def test_save_path_an_existing_card_gains_the_exact_secret(tmp_path):
    harness = _Harness(tmp_path, connections=_connections_with_secrets())
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Connection Hub CLI",
        scopes=["memories:read"], resource=CONCRETE, access_token="at-1", refresh_token="rt-1",
    )
    assert record is not None

    result = await harness.service.extend_client_access(
        ADMIN, client_id=CLIENT, access_id=record.access_id, resource=SECRET,
        claims=[WRITE], resource_operations={SECRET: [WRITE]},
    )

    assert result["ok"] is True, result
    card = harness.persistence.cards[record.access_id][0]
    assert SECRET in card.resource_grants and SECRET_ROW not in card.resource_grants
    assert list(card.resource_grants[SECRET]) == [WRITE]
    assert list(card.resource_operations[SECRET]) == [WRITE]


@pytest.mark.asyncio
async def test_create_path_a_new_card_holds_the_exact_secret(tmp_path):
    harness = _Harness(tmp_path, connections=_connections_with_secrets())

    result = await harness.service.create_access(
        ADMIN, label="Secret writer", resource_grants={SECRET: [WRITE]},
        resource_operations={SECRET: [WRITE]},
    )

    assert result.get("ok") is not False, result
    assert len(harness.persistence.cards) == 1
    card = next(iter(harness.persistence.cards.values()))[0]
    assert list(card.resource_grants) == [SECRET]
    assert list(card.resource_operations[SECRET]) == [WRITE]


def test_the_consent_for_one_secret_never_offers_the_whole_secret_row():
    from connection_hub.delegated_credentials.oauth.consent import resource_selection_rows

    config = oauth_delegated_config_from_connections(_connections_with_secrets())
    for seeded in (None, {SECRET: [WRITE]}):
        rows = resource_selection_rows([WRITE, "memories:read"], config=config, resource=SECRET,
                                       seeded_operations=seeded)
        assert SECRET_ROW not in {row["resource"] for row in rows}, rows


def test_no_consent_offers_the_secret_row_as_a_child():
    from connection_hub.delegated_credentials.oauth.consent import resource_selection_rows

    connections = _connections_with_secrets()
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["resources"][0]["resource_selection"] = True  # a selection door that is not the secret row
    config = oauth_delegated_config_from_connections(connections)

    rows = resource_selection_rows([WRITE, "memories:read"], config=config, resource=CONCRETE)

    assert SECRET_ROW not in {row["resource"] for row in rows}, rows


# Editor Save (update_access) on a CLI Card whose entry is the deployment door (W929 P3).

DEPLOYMENT_ROW = "urn:kdcube:management:deployment:*:*"
DOOR = "urn:kdcube:management:deployment:tenant-a:project-a"
INSPECT = "kdcube.management.deployment.inspect"
USER_PROPS_ROW = "urn:kdcube:management:user-props:*:*"
USER_PROPS_DOOR = "urn:kdcube:management:user-props:tenant-a:project-a"
IMPORT = "kdcube.management.user_props.import"
OTHER_SECRET = "urn:kdcube:management:secret:tenant-a:project-a:bundle:connection-hub@1-0:connections.other_key"


def _connections_with_door_and_secrets():
    connections = _connections_with_secrets()
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["capabilities"].append(
        {"grant": INSPECT, "label": "Inspect deployment", "delegable_roles": ["kdcube:role:super-admin"]}
    )
    oauth["resources"].append({
        "resource": DEPLOYMENT_ROW,
        "label": "KDCube deployment management",
        "admin_only": True,
        "resource_selection": True,
        "operations": {INSPECT: {"grants": [INSPECT]}},
    })
    return connections


async def _door_card(harness):
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Connection Hub CLI",
        scopes=[INSPECT], resource=DOOR, access_token="at-1", refresh_token="rt-1",
    )
    assert record is not None
    return record


async def _editor_save(harness, record, resources):
    return await harness.service.update_access(
        ADMIN, access_id=record.access_id,
        resource_grants={DEPLOYMENT_ROW: [INSPECT], **{resource: [WRITE] for resource in resources}},
        resource_operations={DEPLOYMENT_ROW: [INSPECT], **{resource: [WRITE] for resource in resources}},
    )


@pytest.mark.asyncio
async def test_editor_save_adds_an_exact_secret_to_a_card_entered_through_the_deployment_door(tmp_path):
    harness = _Harness(tmp_path, connections=_connections_with_door_and_secrets())
    record = await _door_card(harness)

    result = await _editor_save(harness, record, [SECRET])

    assert result.get("ok") is not False, result
    card = await harness.card(record.access_id)
    assert SECRET in card.resource_grants and SECRET_ROW not in card.resource_grants
    assert list(card.resource_grants[SECRET]) == [WRITE]
    assert list(card.resource_operations[SECRET]) == [WRITE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource",
    [
        # every secret of one bundle: a selector, not an exact secret
        "urn:kdcube:management:secret:tenant-a:project-a:bundle:connection-hub@1-0:connections.*",
        # an exact secret of another project
        "urn:kdcube:management:secret:tenant-a:project-b:bundle:connection-hub@1-0:connections.signing_key",
    ],
)
async def test_editor_save_still_refuses_a_secret_selector_or_another_projects_secret(tmp_path, resource):
    harness = _Harness(tmp_path, connections=_connections_with_door_and_secrets())
    record = await _door_card(harness)

    result = await _editor_save(harness, record, [resource])

    assert result["ok"] is False, result
    assert result["error"] == "oauth_client_resource_unreachable"
    assert result["resources"] == [resource]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_door", [False, True])
async def test_editor_save_refuses_a_secret_on_a_card_entered_through_a_service_door(tmp_path, selection_door):
    connections = _connections_with_door_and_secrets()
    if selection_door:
        # a selection door that is not a KDCube management door
        connections["delegated_credentials"]["oauth"]["resources"][0]["resource_selection"] = True
    harness = _Harness(tmp_path, connections=connections)
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Connection Hub CLI",
        scopes=["memories:read"], resource=CONCRETE, access_token="at-1", refresh_token="rt-1",
    )
    card = await harness.card(record.access_id)

    result = await harness.service.update_access(
        ADMIN, access_id=record.access_id,
        resource_grants={**{key: list(value) for key, value in card.resource_grants.items()}, SECRET: [WRITE]},
        resource_operations={SECRET: [WRITE]},
    )

    assert result["ok"] is False, result
    assert result["error"] == "oauth_client_resource_unreachable"
    assert result["resources"] == [SECRET]


@pytest.mark.asyncio
async def test_editor_save_reach_does_not_open_the_admin_only_secret_row_to_a_non_admin(tmp_path):
    harness = _Harness(tmp_path, connections=_connections_with_door_and_secrets())
    record = await _door_card(harness)
    registered = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}

    result = await harness.service.update_access(
        registered, access_id=record.access_id,
        resource_grants={DEPLOYMENT_ROW: [INSPECT], SECRET: [WRITE]},
        resource_operations={DEPLOYMENT_ROW: [INSPECT], SECRET: [WRITE]},
    )

    assert result["ok"] is False, result
    assert result["error"] == "delegated_access_grants_not_delegable", result
    assert WRITE in result["grants"], result
    card = await harness.card(record.access_id)
    assert SECRET not in card.resource_grants


@pytest.mark.asyncio
async def test_editor_save_refuses_a_secret_the_catalog_does_not_govern_as_a_secret(tmp_path):
    connections = _connections_with_door_and_secrets()
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["resources"] = [row for row in oauth["resources"] if row.get("selector_type") != "kdcube_secret"]
    # a row that matches secret URNs but is not the catalog's secret row
    oauth["resources"].append({
        "resource": "urn:kdcube:management:secret:*",
        "label": "Not a secret row",
        "admin_only": True,
        "operations": {WRITE: {"grants": [WRITE]}},
    })
    harness = _Harness(tmp_path, connections=connections)
    record = await _door_card(harness)

    result = await _editor_save(harness, record, [SECRET])

    assert result["ok"] is False, result
    assert result["error"] == "oauth_client_resource_unreachable", result
    assert result["resources"] == [SECRET]


@pytest.mark.asyncio
async def test_editor_save_refuses_a_secret_through_a_management_door_that_selects_nothing(tmp_path):
    connections = _connections_with_door_and_secrets()
    for row in connections["delegated_credentials"]["oauth"]["resources"]:
        if row["resource"] == DEPLOYMENT_ROW:
            row["resource_selection"] = False
    harness = _Harness(tmp_path, connections=connections)
    record = await _door_card(harness)

    result = await _editor_save(harness, record, [SECRET])

    assert result["ok"] is False, result
    assert result["error"] == "oauth_client_resource_unreachable", result
    assert result["resources"] == [SECRET]


@pytest.mark.asyncio
async def test_editor_save_refuses_a_secret_on_a_card_entered_through_another_management_door(tmp_path):
    connections = _connections_with_door_and_secrets()
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["capabilities"].append(
        {"grant": IMPORT, "label": "Import user props", "delegable_roles": ["kdcube:role:super-admin"]}
    )
    oauth["resources"].append({
        "resource": USER_PROPS_ROW,
        "label": "KDCube user props",
        "admin_only": True,
        "resource_selection": True,
        "operations": {IMPORT: {"grants": [IMPORT]}},
    })
    harness = _Harness(tmp_path, connections=connections)
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Connection Hub CLI",
        scopes=[IMPORT], resource=USER_PROPS_DOOR, access_token="at-1", refresh_token="rt-1",
    )
    assert record is not None

    result = await harness.service.update_access(
        ADMIN, access_id=record.access_id,
        resource_grants={USER_PROPS_ROW: [IMPORT], SECRET: [WRITE]},
        resource_operations={USER_PROPS_ROW: [IMPORT], SECRET: [WRITE]},
    )

    assert result["ok"] is False, result
    assert result["error"] == "oauth_client_resource_unreachable", result
    assert result["resources"] == [SECRET]


@pytest.mark.asyncio
async def test_editor_save_adds_an_exact_secret_to_a_card_entered_through_one_exact_secret(tmp_path):
    harness = _Harness(tmp_path, connections=_connections_with_door_and_secrets())
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=CLIENT, client_label="Connection Hub CLI",
        scopes=[WRITE], resource=SECRET, access_token="at-1", refresh_token="rt-1",
    )
    assert record is not None

    result = await harness.service.update_access(
        ADMIN, access_id=record.access_id,
        resource_grants={SECRET: [WRITE], OTHER_SECRET: [WRITE]},
        resource_operations={SECRET: [WRITE], OTHER_SECRET: [WRITE]},
    )

    assert result.get("ok") is not False, result
    card = await harness.card(record.access_id)
    assert set(card.resource_grants) == {SECRET, OTHER_SECRET}

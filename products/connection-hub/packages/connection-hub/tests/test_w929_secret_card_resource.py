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

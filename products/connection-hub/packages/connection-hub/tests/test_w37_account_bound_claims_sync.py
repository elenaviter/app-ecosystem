"""W37: a descriptor agent Card whose provider claim is bound on an account, not on the door.

Save accepts it: a provider-backed grant may be present on an exact account_scope binding
instead of resource_grants (docs/connection-hub/package/delegated-cards.md, "Projection into a
saved card"). The next descriptor sync must accept the same Card.
"""
import dataclasses

import pytest

from test_agent_capability_control_sync import NAMED_RESOURCE, OWNER
from test_descriptor_agent_card_transaction import _current
from test_descriptor_agent_consent import POST, READ, _consent_world, _sync_arguments


async def _consent_on_account(service, resident):
    """The hosted Card consent shape: the operation, its door grant named_services:use,
    and the provider claim bound on the approved account only."""
    return await service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Focused consent",
        client_id=resident.client_id, merge_existing=True, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: ["named_services:use"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [POST]}},
        account_scope={"slack": {"approved-workspace": ["slack:post"]}},
        request_id=f"w37-consent-{resident.card_revision}")


@pytest.mark.asyncio
async def test_an_operation_backed_by_an_account_bound_claim_survives_the_descriptor_sync(tmp_path):
    service, store, _, resident, _, _, _ = await _consent_world(tmp_path)
    answer = await _consent_on_account(service, resident)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert "slack:post" not in saved.resource_grants[NAMED_RESOURCE]
    assert saved.account_scope == {"slack": {"approved-workspace": ("slack:post",)}}

    replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
    assert replay["ok"], replay
    synced = await _current(store, resident)
    assert set(synced.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert synced.account_scope == saved.account_scope


@pytest.mark.asyncio
async def test_the_synced_card_projects_the_bridge_tool_for_the_account_backed_operation(tmp_path):
    """The full path: consent with the claim on the account, the next sync, then the projected tools."""
    service, store, _, resident, _, _, _ = await _consent_world(tmp_path)
    assert (await _consent_on_account(service, resident))["ok"]
    replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
    assert replay["ok"], replay
    synced = await _current(store, resident)
    assert "named_services_call" in synced.resource_operations[NAMED_RESOURCE]
    assert "named_services_call" in replay["card"]["resource_operations"][NAMED_RESOURCE]


@pytest.mark.asyncio
async def test_an_account_claim_the_catalog_does_not_declare_does_not_back_an_operation(tmp_path):
    """Only provider-backed grants the catalog declares for the resource count from account_scope."""
    service, store, _, resident, _, _, _ = await _consent_world(tmp_path)
    assert (await _consent_on_account(service, resident))["ok"]
    saved = await _current(store, resident)
    # The account keeps only a claim the catalog does not declare for this resource.
    unrelated = dataclasses.replace(saved, account_scope={"slack": {"approved-workspace": ("slack:admin",)}})
    from connection_hub.delegated_credentials.agent_capability_sync import resolve_agent_card_standard_authority
    active = await service._active_catalog()
    resolved = await resolve_agent_card_standard_authority(
        service, active=active, owner_subject=OWNER,
        resource_grants={NAMED_RESOURCE: list(unrelated.resource_grants[NAMED_RESOURCE])},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ, POST]}},
        properties={}, account_scope=unrelated.account_scope)
    assert resolved.error and resolved.error["error"] == "agent_descriptor_authority_invalid"
    assert "object.action.post_message" in resolved.error["message"]


@pytest.mark.asyncio
async def test_the_control_ceiling_counts_door_grants_only_and_takes_no_account(tmp_path):
    """The credentialless ceiling has no account parameter: an account binding never widens it."""
    import inspect

    from connection_hub.delegated_credentials.agent_capability_sync import (
        resolve_agent_card_standard_authority, resolve_descriptor_control_ceiling,
    )
    assert "account_scope" not in inspect.signature(resolve_descriptor_control_ceiling).parameters
    agent = inspect.signature(resolve_agent_card_standard_authority).parameters["account_scope"]
    assert agent.default is inspect.Parameter.empty, "the agent Card must name its binding"
    service, _, _, _, _, _, _ = await _consent_world(tmp_path)
    active = await service._active_catalog()
    refused = await resolve_descriptor_control_ceiling(
        service, active=active, owner_subject=OWNER,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ, POST]}}, properties={})
    assert refused.error and "object.action.post_message" in refused.error["message"]

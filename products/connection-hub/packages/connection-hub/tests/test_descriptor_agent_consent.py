"""A focused resident consent and its saved descriptor selection are one revision."""

import pytest

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_DEFAULTS_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY,
)
from test_agent_capability_control_sync import (
    AGENT, APPLICATION, NAMED_RESOURCE, OWNER, _policy_with_named_operations, _service,
)
from test_descriptor_agent_card_transaction import _current, _world

READ = "object.list"
POST = "object.action.post_message"


def _sync_arguments(*, allow_post=True):
    operations = [READ, POST] if allow_post else [READ]
    return {
        "application": APPLICATION, "agent_id": AGENT, "descriptor_revision": "descriptor-r1",
        "descriptor_payload": {"revision": "descriptor-r1"},
        "capability_authority": _policy_with_named_operations(*operations),
        "resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:read", *(["slack:post"] if allow_post else [])]},
        "resource_operations": {NAMED_RESOURCE: ["named_services_call"]},
        "named_service_operations": {NAMED_RESOURCE: {"slack": operations}},
    }


async def _consent_world(tmp_path, *, allow_post=True):
    service, persistence = _service(named_services=True)
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **_sync_arguments(allow_post=allow_post),
        selected_capabilities=_policy_with_named_operations(READ),
        # The synthetic descriptor has no entries from which the trusted
        # resolver could derive standard defaults. Supply the READ selection
        # explicitly, as a real descriptor's resolved defaults would do.
        selected_resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        selected_resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        selected_named_service_operations={NAMED_RESOURCE: {"slack": [READ]}})
    assert answer["ok"], answer
    resident = persistence.records[answer["card"]["access_id"]][0]
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert set(resident.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ}
    return await _world(tmp_path, pair=(service, resident, control))


async def _consent(service, resident, *, merge=True, operation=POST, account_scope=None):
    return await service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Focused consent",
        client_id=resident.client_id, merge_existing=merge, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post" if operation == POST else "slack:read"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [operation]}},
        account_scope=account_scope,
        request_id=f"focused-consent-{resident.card_revision}-{operation}")


def _selection(card):
    return card.properties[AGENT_CAPABILITY_SELECTION_PROPERTY]["capabilities"]["named_service_operations"]


@pytest.mark.asyncio
async def test_focused_consent_commits_selection_with_grants_and_survives_repeated_sync(tmp_path, monkeypatch):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)

    async def mint(*args, **kwargs):
        raise AssertionError("a live resident consent keeps its bearer")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await _consent(service, resident)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert saved.card_revision == resident.card_revision + 1
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert set(_selection(saved)) == {f"slack/{READ}", f"slack/{POST}"}
    assert decisions.decisions == ["committed"] and service._persistence.direct == []
    for _ in range(3):
        replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
        assert replay["ok"], replay
        assert await _current(store, resident) == saved
    assert decisions.decisions == ["committed"]


@pytest.mark.asyncio
async def test_focused_consent_saves_approved_account_binding_with_selection_and_keeps_it_on_sync(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    approved = {"slack": {"approved-workspace": ["slack:post"]}}
    answer = await _consent(service, resident, account_scope=approved)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert saved.card_revision == resident.card_revision + 1
    assert saved.account_scope == {"slack": {"approved-workspace": ("slack:post",)}}
    assert set(_selection(saved)) == {f"slack/{READ}", f"slack/{POST}"}
    assert decisions.decisions == ["committed"] and service._persistence.direct == []

    # The next focused consent names no account. Cumulative consent and normal
    # sync both retain the exact account/claim the person already approved.
    answer = await _consent(service, saved, operation=READ)
    assert answer["ok"], answer
    cumulative = await _current(store, resident)
    assert cumulative.account_scope == saved.account_scope
    for _ in range(3):
        replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
        assert replay["ok"], replay
        assert await _current(store, resident) == cumulative
    assert decisions.decisions == ["committed", "committed"]


@pytest.mark.asyncio
async def test_owner_screen_permission_save_without_properties_commits_and_aligns_selection(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    answer = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=resident.access_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read", "slack:post"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ, POST]}},
        expected_card_revision=resident.card_revision, expected_catalog_version=resident.catalog_version,
        invocation_modes={}, _screen_save=True, request_id="owner-permission-only-save")
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert saved.card_revision == resident.card_revision + 1
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert set(_selection(saved)) == {f"slack/{READ}", f"slack/{POST}"}
    assert decisions.decisions == ["committed"] and service._persistence.direct == []


@pytest.mark.asyncio
async def test_owner_screen_explicit_property_change_still_refuses_by_name(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    answer = await service.update_access(
        {"user_id": OWNER}, access_id=resident.access_id,
        resource_grants=resident.resource_grants, resource_operations=resident.resource_operations,
        named_service_operations=resident.named_service_operations.to_stored(),
        properties={**resident.properties, "explicit-property-edit": True},
        expected_card_revision=resident.card_revision, expected_catalog_version=resident.catalog_version,
        invocation_modes={}, _screen_save=True, request_id="owner-explicit-property-save")
    assert answer["ok"] is False and answer["error"] == "managed_card_edit_fields_unsupported"
    assert await _current(store, resident) == resident
    assert decisions.decisions == [] and service._persistence.direct == []


@pytest.mark.asyncio
async def test_replace_consent_removes_the_saved_choice_and_sync_does_not_reintroduce_it(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    assert (await _consent(service, resident))["ok"]
    saved = await _current(store, resident)
    answer = await _consent(service, saved, merge=False, operation=READ)
    assert answer["ok"], answer
    narrowed = await _current(store, resident)
    assert set(narrowed.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ}
    assert set(_selection(narrowed)) == {f"slack/{READ}"}
    # Replace also removed the outer bridge. Passive sync must retain this
    # independent restriction even though the inner READ choice remains.
    assert not narrowed.resource_operations.get(NAMED_RESOURCE)
    for _ in range(3):
        replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
        assert replay["ok"], replay
        unchanged = await _current(store, resident)
        assert unchanged == narrowed
        assert not unchanged.resource_operations.get(NAMED_RESOURCE)
    assert decisions.decisions == ["committed", "committed"]


@pytest.mark.asyncio
async def test_new_descriptor_default_stays_unselected_and_withdrawn_choice_restores_without_new_grants(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path, allow_post=False)
    for revision, offered, visible in (
        ("descriptor-add-post-default", [READ, POST], {f"slack/{READ}"}),
        ("descriptor-withdraw-read", [POST], set()),
        ("descriptor-restore-read", [READ, POST], {f"slack/{READ}"}),
    ):
        arguments = _sync_arguments()
        arguments.update(
            descriptor_revision=revision,
            descriptor_payload={"revision": revision, "capability_defaults": _policy_with_named_operations(POST)},
            capability_authority=_policy_with_named_operations(*offered),
            resource_grants={NAMED_RESOURCE: ["named_services:use", *(
                "slack:read" if operation == READ else "slack:post" for operation in offered)]},
            named_service_operations={NAMED_RESOURCE: {"slack": offered}})
        answer = await service.sync_agent_capability_control({"user_id": OWNER}, **arguments)
        assert answer["ok"], answer
        saved = await _current(store, resident)
        assert saved.resource_grants == resident.resource_grants
        assert saved.resource_operations == resident.resource_operations
        assert saved.named_service_operations == resident.named_service_operations
        assert set(_selection(saved)) == {f"slack/{READ}"}
        assert set(answer["projection"]["capabilities"].get("named_service_operations", ())) == visible
        defaults = answer["control_card"]["properties"][AGENT_CAPABILITY_DEFAULTS_PROPERTY]
        assert defaults["capabilities"]["named_service_operations"] == [f"slack/{POST}"]
    assert decisions.decisions == ["committed"] * 3
    replay = await service.sync_agent_capability_control({"user_id": OWNER}, **arguments)
    assert replay["ok"] and await _current(store, resident) == saved
    assert decisions.decisions == ["committed"] * 3


@pytest.mark.asyncio
async def test_consent_outside_descriptor_control_refuses_before_mint_or_commit(tmp_path, monkeypatch, caplog):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path, allow_post=False)

    async def mint(*args, **kwargs):
        raise AssertionError("an out-of-ceiling consent must not mint")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await _consent(service, resident)
    assert answer["ok"] is False and answer["error"] == "agent_capability_selection_outside_control"
    assert {"resource": NAMED_RESOURCE, "namespace": "slack", "operation": POST} in answer["details"]["outside"]
    assert any("agent_capability_selection_outside_control" in record.getMessage()
               and POST in record.getMessage() for record in caplog.records)
    assert await _current(store, resident) == resident
    assert decisions.decisions == [] and service._persistence.direct == []


def test_control_refusal_details_are_bounded_catalog_coordinates_only(caplog):
    from connection_hub.delegated_credentials.descriptor_agent_card import DescriptorAgentCardRefused

    refusal = DescriptorAgentCardRefused("agent_capability_selection_outside_control", status=403, outside=[
        {"resource": NAMED_RESOURCE, "namespace": "slack", "operation": POST,
         "account_id": "private-account", "message": "private free text"},
        {"resource": NAMED_RESOURCE, "operation": "credential=secret with whitespace"},
        *({"resource": NAMED_RESOURCE, "operation": f"operation.{i}"} for i in range(40)),
    ])
    details = refusal.to_dict()["details"]
    assert len(details["outside"]) == 32
    assert details["outside_count"] == 42 and details["outside_truncated"] is True
    assert set(details["outside"][0]) == {"resource", "namespace", "operation"}
    rendered = repr(details) + " ".join(record.getMessage() for record in caplog.records)
    assert "private-account" not in rendered and "private free text" not in rendered
    assert "secret with whitespace" not in rendered

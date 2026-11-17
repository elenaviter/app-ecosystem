"""W38: one ceiling rule for named-service object.action (kdcube#270 D1, D4).

| descriptor for a namespace       | action ceiling                         |
| object.action only               | every action of the namespace in the Hub catalog |
| object.action + list             | every action in the Hub catalog        |
| list only                        | exactly the listed actions             |

The standard part and the capability ceiling of a descriptor Control are equal.
"""
import pytest

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_DEFAULTS_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    CAPABILITY_ALLOWED_SELECTED,
)
from test_agent_capability_control_sync import (
    AGENT, APPLICATION, NAMED_RESOURCE, OWNER, _policy_with_named_operations, _service,
)

LIST = "object.list"
PARENT = "object.action"
POST = "object.action.post_message"


def _arguments(*declared: str):
    return {
        "application": APPLICATION, "agent_id": AGENT, "descriptor_revision": "descriptor-" + "-".join(declared),
        # The SDK sends what the bundle declares; Connection Hub resolves it against its catalog.
        "descriptor_payload": {
            "revision": "descriptor-" + "-".join(declared),
            "standard_authority": {"named_services": [{"namespace": "slack", "operations": list(declared)}]},
        },
        "capability_authority": _policy_with_named_operations(*declared),
    }


async def _sync(service, *declared: str, selected=None):
    return await service.sync_agent_capability_control(
        {"user_id": OWNER}, **_arguments(*declared),
        selected_capabilities=selected if selected is not None else _policy_with_named_operations(*declared))


def _keys(record, prop):
    return set(record.properties[prop]["capabilities"].get("named_service_operations", ()))


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT), (LIST, PARENT, POST)], ids=["parent-only", "parent-plus-list"])
async def test_a_parent_object_action_expands_the_capability_ceiling_to_every_catalog_action(declared):
    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    standard = set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"])
    assert POST in standard
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "the ceiling equals the standard part"
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_DEFAULTS_PROPERTY)
    # The parent stays allowed: the SDK reads it as the switch for the action call itself.
    assert f"slack/{PARENT}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert answer["states"]["named_service_operations"].get(f"slack/{PARENT}") == CAPABILITY_ALLOWED_SELECTED
    # D1a: the picker state of a concrete action is decided by the expanded ceiling.
    assert answer["states"]["named_service_operations"].get(f"slack/{POST}") == CAPABILITY_ALLOWED_SELECTED


@pytest.mark.asyncio
async def test_a_list_without_the_parent_is_exactly_the_listed_actions():
    service, persistence = _service(named_services=True)
    answer = await _sync(service, LIST, POST)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert f"slack/{PARENT}" not in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT), (LIST, PARENT, POST)], ids=["parent-only", "parent-plus-list"])
async def test_an_agent_card_editor_save_keeps_the_actions_of_a_parent_only_descriptor(tmp_path, declared):
    """D1c: aligning the base after a Save intersects the Card's concrete actions with the ceiling."""
    from test_descriptor_agent_card_transaction import _current, _world

    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    resident = persistence.records[answer["card"]["access_id"]][0]
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert POST in resident.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    service, store, _, resident, _, _, _ = await _world(tmp_path, pair=(service, resident, control))
    saved = await service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Editor save",
        client_id=resident.client_id, merge_existing=False, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: list(resident.resource_grants[NAMED_RESOURCE])},
        resource_operations={NAMED_RESOURCE: list(resident.resource_operations[NAMED_RESOURCE])},
        named_service_operations={NAMED_RESOURCE: {"slack": sorted(resident.named_service_operations.operations[NAMED_RESOURCE]["slack"])}},
        request_id="w38-editor-save")
    assert saved["ok"], saved
    after = await _current(store, resident)
    assert f"slack/{POST}" in _keys(after, AGENT_CAPABILITY_SELECTION_PROPERTY), "the Save keeps the action in the base"
    replay = await _sync(service, *declared, selected=None)
    assert replay["ok"], replay
    synced = await _current(store, resident)
    assert POST in synced.named_service_operations.operations[NAMED_RESOURCE]["slack"], "and the next sync keeps it on the Card"


@pytest.mark.asyncio
async def test_a_props_override_from_an_older_sdk_does_not_narrow_the_control():
    """Only the stored Control Card narrows. Before W757 a Control save also wrote
    bundle props, and an SDK that still reads them marks the payload overridden and
    sends the narrower list: the descriptor's own standard part still counts."""
    service, persistence = _service(named_services=True)
    arguments = _arguments(LIST, PARENT)
    arguments["descriptor_payload"] = {**arguments["descriptor_payload"], "standard_authority_overridden": True}
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **arguments,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [LIST]}},
        selected_capabilities=_policy_with_named_operations(LIST, PARENT))
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)


@pytest.mark.asyncio
async def test_a_human_edit_of_the_descriptor_control_narrows_its_capability_ceiling():
    """An administrator Save on the descriptor Control is an explicit list too."""
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, PARENT)
    assert created["ok"], created
    control_id = created["control_card"]["access_id"]
    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=control_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]}, resource_operations={},
        named_service_operations={NAMED_RESOURCE: {"slack": [LIST]}}, account_scope={},
        expected_card_revision=created["control_card"]["card_revision"],
        expected_catalog_version=created["control_card"]["catalog_version"],
        composition_mode="and", properties=created["control_card"]["properties"])
    assert changed["ok"], changed
    saved = persistence.records[control_id][0]
    assert f"slack/{POST}" not in _keys(saved, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "right after the Save"
    refreshed = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **{**_arguments(LIST, PARENT), "descriptor_revision": "descriptor-r2",
                               "descriptor_payload": {**_arguments(LIST, PARENT)["descriptor_payload"], "revision": "descriptor-r2"}})
    assert refreshed["ok"], refreshed
    control = persistence.records[control_id][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST}
    assert f"slack/{POST}" not in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "and after a descriptor refresh"


@pytest.mark.asyncio
async def test_a_props_override_does_not_intersect_an_administrator_save():
    """The administrator keeps both operations; an older SDK then sends a leftover
    props override listing only the action. The stored Save wins, and nothing is
    lost for good once the leftover goes away."""
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, PARENT)
    control_id = created["control_card"]["access_id"]
    both = {NAMED_RESOURCE: {"slack": [LIST, POST]}}
    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=control_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read", "slack:post"]},
        resource_operations={}, named_service_operations=both, account_scope={},
        expected_card_revision=created["control_card"]["card_revision"],
        expected_catalog_version=created["control_card"]["catalog_version"],
        composition_mode="and", properties=created["control_card"]["properties"])
    assert changed["ok"], changed
    stale = _arguments(LIST, PARENT)
    stale["descriptor_revision"] = "descriptor-with-props"
    stale["descriptor_payload"] = {**stale["descriptor_payload"], "revision": "descriptor-with-props",
                                   "standard_authority_overridden": True}
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **stale,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [POST]}})
    assert answer["ok"], answer
    control = persistence.records[control_id][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}



HOST_FILE = "object.host_file"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT, HOST_FILE), (LIST, PARENT, POST, HOST_FILE)], ids=["parent-only", "parent-plus-list"])
async def test_an_operation_outside_the_hub_catalog_stays_in_the_ceiling(declared):
    """Live W38 regression: the descriptor also allows an SDK-side operation the Hub
    catalog does not list (object.host_file). Bounding the ceiling to the standard part
    narrows only object.action keys, so the operation stays and the selection the SDK
    sends is not refused as agent_capability_selection_outside_control."""
    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert HOST_FILE not in set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"])
    ceiling = _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert f"slack/{HOST_FILE}" in ceiling and f"slack/{POST}" in ceiling and f"slack/{PARENT}" in ceiling, ceiling
    replay = await _sync(service, *declared)
    assert replay["ok"], replay


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", [
    (LIST, PARENT, "object.action.add_reaction"),
    (LIST, "object.action.add_reaction"),
], ids=["administrator-narrowing-keeps-the-parent", "descriptor-list-only"])
async def test_a_narrowed_action_stays_on_the_agent_card_as_provenance(ceiling):
    """kdcube#270 contract item 10, whichever way the Control narrows.

    The sync's standard-part step keeps a saved choice the ceiling no longer
    governs. An administrator narrowing keeps the bare object.action parent
    while the namespace keeps any action; read as a pattern, the parent made the
    removed action governed and dropped the owner's saved choice, while a
    descriptor list without the parent kept it."""
    import dataclasses

    from connection_hub.delegated_credentials.agent_capability_policy import AgentCapabilityPolicy
    from connection_hub.delegated_credentials.cards.model import NamedServiceSelection
    from connection_hub.delegated_credentials.descriptor_agent_selection import selected_standard_maps

    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, POST)
    control_id = created["control_card"]["access_id"]
    card = next(rows[0] for access_id, rows in persistence.records.items() if access_id != control_id)
    assert POST in card.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    control = dataclasses.replace(persistence.records[control_id][0],
        named_service_operations=NamedServiceSelection.exact({NAMED_RESOURCE: {"slack": [LIST]}}))
    selection = card.properties[AGENT_CAPABILITY_SELECTION_PROPERTY]
    authority = AgentCapabilityPolicy.from_property({**selection, "capabilities": {
        "named_services": ["slack"], "named_service_operations": [f"slack/{key}" for key in ceiling]}})
    config = await service._catalog_config(await service._active_catalog(), owner_subject=OWNER)
    _grants, _operations, named = selected_standard_maps(
        resident=card, control=control, authority=authority,
        selected=AgentCapabilityPolicy.from_property(selection), config=config, allow_additions=False)
    assert set(named.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}


class _FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)


async def _administrator_keeps_only_list(service, created):
    card = created["control_card"]
    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=card["access_id"],
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]}, resource_operations={},
        named_service_operations={NAMED_RESOURCE: {"slack": [LIST]}}, account_scope={},
        expected_card_revision=card["card_revision"], expected_catalog_version=card["catalog_version"],
        composition_mode="and", properties=card["properties"])
    assert changed["ok"], changed


@pytest.mark.asyncio
async def test_the_agent_card_view_names_what_the_control_no_longer_allows():
    """W38 visibility: the choice stays on the Card as provenance, and the
    owner sees which choices the Control excludes and who narrowed it."""
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, POST)
    control_id = created["control_card"]["access_id"]
    await _administrator_keeps_only_list(service, created)
    agent = next(rows[0] for access_id, rows in persistence.records.items() if access_id != control_id)
    from connection_hub.delegated_credentials.automation_access import record_from_card

    view = await service._effective_control_view(record_from_card(agent))

    outside = view["outside_control"]
    assert outside["by"] == "administrator"
    assert outside["named_service_operations"] == {NAMED_RESOURCE: {"slack": [POST]}}
    assert "slack:post" in outside["claims"][NAMED_RESOURCE]


@pytest.mark.asyncio
async def test_a_refused_sync_is_recorded_for_the_owner_and_cleared_by_the_next_success():
    """W38 visibility: a refused sync changes no Card, so it is recorded."""
    service, _persistence = _service(named_services=True)
    service._redis = _FakeRedis()
    created = await _sync(service, LIST, POST)
    assert created["ok"], created
    refused = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **{**_arguments(LIST, POST), "capability_authority": {"schema": "broken"}})
    assert refused["ok"] is False
    store = service._agent_sync_outcomes()
    recorded = await store.read(grantor_subject=OWNER, application=APPLICATION, agent_id=AGENT)
    assert recorded and recorded["error"] == refused["error"]
    shown = await service.control_card_get(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, control_id=created["control_card"]["access_id"])
    assert shown["control_card"]["agent_sync_refusal"]["error"] == refused["error"]

    again = await _sync(service, LIST, POST)
    assert again["ok"], again
    assert await store.read(grantor_subject=OWNER, application=APPLICATION, agent_id=AGENT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("control_keeps_post", [False, True], ids=["control-excludes-the-claim", "only-the-card-lacks-it"])
async def test_a_claim_the_control_excludes_is_not_asked_of_the_user(control_keeps_post):
    """W38: the credential carries only effective claims, but the guard keeps the
    live composition for the request. A missing claim the Control excludes gets
    the Control-blocked denial; a claim only the Agent Card lacks still asks."""
    import dataclasses
    from types import SimpleNamespace

    from connection_hub.delegated_credentials.automation_access import card_authority_from_record
    from connection_hub.delegated_credentials.consent_denial import agent_grant_consent_denial
    from connection_hub.delegated_credentials.controls.attribution import ResolvedCardComposition
    from connection_hub.named_service_admission import MANAGED_ADMISSION_STATE_ATTR

    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, POST)
    control_id = created["control_card"]["access_id"]
    caller = card_authority_from_record(
        next(rows[0] for access_id, rows in persistence.records.items() if access_id != control_id))
    control = persistence.records[control_id][0]
    if not control_keeps_post:
        control = dataclasses.replace(control, resource_grants={NAMED_RESOURCE: ("named_services:use", "slack:read")})
    caller = dataclasses.replace(caller, resource_grants={NAMED_RESOURCE: (
        ("named_services:use", "slack:read", "slack:post") if not control_keeps_post
        else ("named_services:use", "slack:read"))})
    effective = dataclasses.replace(caller, resource_grants={NAMED_RESOURCE: ("named_services:use", "slack:read")})
    snapshot = SimpleNamespace(
        card_composition=ResolvedCardComposition(caller_card=caller, effective_card=effective,
                                                 control_card=card_authority_from_record(control)),
        resource=NAMED_RESOURCE, request_resource="", access_id=caller.access_id,
        card_revision=caller.card_revision, card_catalog_version=caller.catalog_version)
    request = SimpleNamespace(state=SimpleNamespace(**{
        MANAGED_ADMISSION_STATE_ATTR: snapshot,
        "delegated_credential": {"credential": {"attrs": {"grants": ["named_services:use", "slack:read"],
                                                          "resource": NAMED_RESOURCE}},
                                 "grant_record": {"client_id": caller.client_id, "grants": [],
                                                  "registry_access_id": caller.access_id}}}))

    denial = agent_grant_consent_denial(
        request, namespace="slack", tool="named_services_action", operation=POST,
        required=["named_services:use", "slack:post"], missing=["slack:post"],
        available=["named_services:use", "slack:read"])

    if control_keeps_post:
        assert "consent" in denial
    else:
        assert "consent" not in denial
        assert denial["ret"]["recovery"]["request_user_consent"] is False
        assert denial["missing_grants"] == ["slack:post"]


@pytest.mark.asyncio
async def test_return_to_descriptor_hands_the_control_back_to_the_descriptor():
    """W38: an administrator's Save only ever narrows on later descriptor
    refreshes; the explicit way back clears the edited mark, and the next sync
    seeds the Control from the descriptor again."""
    from connection_hub.delegated_credentials.agent_capability_control import (
        AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY,
    )

    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, POST)
    control_id = created["control_card"]["access_id"]
    await _administrator_keeps_only_list(service, created)

    def control():
        return persistence.records[control_id][0]

    assert control().provenance[AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY] is True
    refreshed = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **{**_arguments(LIST, POST), "descriptor_revision": "descriptor-r2",
                               "descriptor_payload": {**_arguments(LIST, POST)["descriptor_payload"], "revision": "descriptor-r2"}})
    assert refreshed["ok"], refreshed
    assert set(control().named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST}, \
        "an edited Control never widens on a refresh"

    admin = {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}
    returned = await service.return_control_to_descriptor(
        admin, access_id=control_id, expected_card_revision=control().card_revision,
        expected_catalog_version=control().catalog_version)
    assert returned["ok"], returned
    assert AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY not in control().provenance

    again = await _sync(service, LIST, POST)
    assert again["ok"], again
    assert set(control().named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}
    assert f"slack/{POST}" in _keys(control(), AGENT_CAPABILITY_AUTHORITY_PROPERTY)

    twice = await service.return_control_to_descriptor(
        admin, access_id=control_id, expected_card_revision=control().card_revision)
    assert twice["ok"] is False and twice["error"] == "control_card_already_follows_descriptor"


@pytest.mark.asyncio
async def test_only_an_administrator_returns_a_control_to_its_descriptor():
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, POST)
    control_id = created["control_card"]["access_id"]
    await _administrator_keeps_only_list(service, created)
    control = persistence.records[control_id][0]
    refused = await service.return_control_to_descriptor(
        {"user_id": OWNER}, access_id=control_id, expected_card_revision=control.card_revision)
    assert refused["ok"] is False
    assert persistence.records[control_id][0].card_revision == control.card_revision


@pytest.mark.asyncio
async def test_a_passive_sync_under_a_narrowed_control_is_not_refused(tmp_path):
    """Live 2026-11-17: once the descriptor allowed more than an administrator-
    narrowed Control, every passive sync re-sent the descriptor default and was
    refused as outside the Control, so the agent ran closed. Only an explicit
    selection save is checked; the Control stays narrowed."""
    from test_descriptor_agent_card_transaction import _current
    from test_descriptor_agent_consent import READ, _consent_world, _sync_arguments

    service, store, _decisions, resident, control, _, _ = await _consent_world(tmp_path)
    admin = {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}
    narrowed = await service.update_access(
        admin, access_id=control.access_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ]}}, account_scope={},
        expected_card_revision=control.card_revision, expected_catalog_version=control.catalog_version,
        composition_mode="and", properties=control.properties, request_id="admin-narrows")
    assert narrowed["ok"], narrowed
    wider = {**_sync_arguments(), "descriptor_revision": "descriptor-r2",
             "descriptor_payload": {"revision": "descriptor-r2"}}
    passive = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **wider, selected_capabilities=_policy_with_named_operations(READ, POST))
    assert passive["ok"], passive
    assert POST not in (await _current(store, resident)).named_service_operations.operations[NAMED_RESOURCE]["slack"]

    explicit = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **wider,
        selected_capabilities=_policy_with_named_operations(READ, POST), replace_selection=True)
    assert explicit["ok"] is False and explicit["error"] == "agent_capability_selection_outside_control"


@pytest.mark.asyncio
async def test_an_owner_save_keeps_the_choices_the_control_no_longer_allows(tmp_path):
    """kdcube#270 item 10, editor Save: the editor shows a held action outside the
    Control and does not send it; the Save must not read that as a removal, or the
    choice is lost for good (a passive sync never re-adds it)."""
    from test_descriptor_agent_card_transaction import _current
    from test_descriptor_agent_consent import READ, _consent, _consent_world, _selection, _sync_arguments

    service, store, _decisions, resident, control, _, _ = await _consent_world(tmp_path)
    consented = await _consent(service, resident)
    assert consented["ok"], consented
    resident = await _current(store, resident)
    admin = {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}
    narrowed = await service.update_access(
        admin, access_id=control.access_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ]}}, account_scope={},
        expected_card_revision=control.card_revision, expected_catalog_version=control.catalog_version,
        composition_mode="and", properties=control.properties, request_id="admin-narrows")
    assert narrowed["ok"], narrowed
    synced = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
    assert synced["ok"], synced
    resident = await _current(store, resident)

    saved = await service.update_access(
        admin, access_id=resident.access_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [READ]}},
        expected_card_revision=resident.card_revision, expected_catalog_version=resident.catalog_version,
        invocation_modes={}, _screen_save=True, request_id="owner-save")
    assert saved["ok"], saved
    after = await _current(store, resident)
    assert set(after.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert "slack:post" in after.resource_grants[NAMED_RESOURCE]
    assert f"slack/{POST}" in _selection(after)

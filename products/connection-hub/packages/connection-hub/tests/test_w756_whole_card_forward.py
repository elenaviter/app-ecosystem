"""Real Hub forward entry with a recording peer/catalog; not productive PB qualification."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from test_w638_managed_card_edit_forward import Host, service
from test_w738_managed_my_card_settings import original, ACTOR, PROJECT
from test_w756_whole_card_diff import Catalog


def setup():
    peer = Host()
    host = service(peer)
    record = record_from_card(replace(original(), resource_grants={"service": ("read",)},
                                      resource_operations={"service": ("read",)}))
    host._managed_direct_write_refused = lambda: {"ok": False, "error": "direct_refused"}
    reads = []

    async def current(*args, **kwargs):
        reads.append("card")
        return record, "active"

    async def active():
        reads.append("catalog")
        return SimpleNamespace(version="render-29")

    async def config(*args, **kwargs):
        return Catalog()

    host._load_record_any_state, host._active_catalog, host._catalog_config = current, active, config
    return peer, host, record, reads


async def save(host, record, **changes):
    fields = dict(record=record, request_id=None, expected_card_revision=record.card_revision,
        expected_catalog_version="render-29", selection={"resource_grants": {"service": ["read"]},
        "resource_operations": {"service": ["read"]}, "account_scope": {}},
        properties=copy.deepcopy(record.properties), composition_mode=None, label=record.label,
        accepted_operations=None, invocation_modes={})
    fields.update(changes)
    return await host._forward_own_my_card_edit({"user_id": ACTOR}, **fields)


@pytest.mark.asyncio
async def test_identical_whole_card_is_ok_without_a_forward():
    peer, host, record, reads = setup()
    assert await save(host, record) == {"ok": True, "unchanged": True}
    assert reads == ["card", "catalog"] and peer.calls == []


@pytest.mark.asyncio
async def test_stale_card_wins_even_for_identical_content_and_bad_catalog_pin():
    peer, host, record, reads = setup()
    answer = await save(host, record, expected_card_revision=record.card_revision - 1,
                        expected_catalog_version="old")
    assert answer["error"] == "managed_card_edit_revision_changed"
    assert reads == ["card"] and peer.calls == []


@pytest.mark.asyncio
async def test_catalog_pin_without_accept_update_is_refused_before_a_forward():
    peer, host, record, reads = setup()
    answer = await save(host, record, expected_catalog_version="old")
    assert answer["error"] == "consent_catalog_changed"
    assert answer["expected_catalog_version"] == "old" and answer["active_catalog_version"] == "render-29"
    assert peer.calls == []


@pytest.mark.asyncio
async def test_screen_save_without_catalog_pin_is_refused_not_silently_accepted():
    peer, host, record, reads = setup()
    answer = await save(host, record, expected_catalog_version=None, _screen_save=True)
    assert answer["error"] == "consent_catalog_version_required" and peer.calls == []
    assert reads == ["card", "catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["person_control", "invitation_control", "project_control"])
async def test_whole_control_property_changes_are_refused_before_forward(kind):
    peer, host, record, reads = setup()
    from connection_hub.delegated_credentials.managed_card_edit_forward import ManagedCardEditError
    properties = {**dict(record.properties), "changed": "not allowed"}
    with pytest.raises(ManagedCardEditError, match="managed_card_edit_fields_unsupported"):
        await host._whole_card_save_preflight(record=record, kind=kind, expected_card_revision=record.card_revision,
            expected_catalog_version="render-29", selection={"resource_operations": {"service": ["read"]}},
            properties=properties)
    assert peer.calls == []


@pytest.mark.asyncio
async def test_whole_all_adds_modes_and_rename_are_one_signed_forward():
    peer, host, record, reads = setup()
    answer = await save(host, record,
        selection={"resource_grants": {"service": ["read"]}, "resource_operations": {"service": ["*"]},
                   "account_scope": {}}, label="Whole renamed Card",
        invocation_modes={"service": {"write": "always", "delete": "once"}})
    assert answer["ok"] is True and len(peer.calls) == 1
    body = peer.calls[0][2]
    assert body["target"]["original_revision"] == record.card_revision
    assert body["expected_catalog_version"] == "render-29"
    assert body["selection"]["resource_operations"] == {"service": ["delete", "read", "write"]}
    assert body["selection"]["properties"] == dict(record.properties)
    assert body["invocation_modes"] == {"service": {"delete": "once", "write": "always"}}
    assert body["label"] == "Whole renamed Card"


@pytest.mark.asyncio
async def test_missing_addition_choice_keeps_the_whole_draft_without_forward():
    peer, host, record, reads = setup()
    selection = {"resource_operations": {"service": ["read", "write", "delete"]}}
    answer = await save(host, record, selection=selection, invocation_modes={"service": {"write": "always"}})
    assert answer["error"] == "invocation_mode_required"
    assert answer["missing_invocation_modes"] == {"service": ["delete"]}
    assert selection == {"resource_operations": {"service": ["read", "write", "delete"]}}
    assert peer.calls == []

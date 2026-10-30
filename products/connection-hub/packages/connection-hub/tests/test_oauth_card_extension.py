# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A consent link extends the exact OAuth card it was denied on.

An OAuth card id is derived from the concrete URL the client consented at,
while its grants and the denial name the declared door. The link therefore
carries the card id; without it the hub derives a different id and finds no
card to extend."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.automation_access import oauth_access_id
from test_resident_profile_cards import GRANTOR, MEMORIES, USER, _Harness, _connections

CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"
CONCRETE = "https://host/api/mcp/memories"
NAMED = "https://host/api/mcp/named-services"
OTHER_NAMED = "https://host/api/mcp/other-services"
SLIDES = ["object.get", "object.search", "object.action.compose"]
UPLOAD = "object.action.request_upload"
GRANTS = ["named_services:use", "slides:use", "mail:send"]


def _named_connections(*, newer_catalog=False):
    connections = _connections()
    oauth = connections["delegated_credentials"]["oauth"]
    oauth["capabilities"].extend(
        {"grant": grant, "label": grant, "delegable_roles": ["kdcube:role:registered"]}
        for grant in GRANTS
    )
    operations = SLIDES + (["object.action.delete"] if newer_catalog else [])
    for resource, namespaces in (
        (NAMED, (("slides", "slides:use", operations), ("mail", "mail:send", [UPLOAD]))),
        (OTHER_NAMED, (("notes", "slides:use", ["object.get"]),)),
    ):
        oauth["resources"].append({
            "resource": resource,
            "label": "Named services",
            "grants": GRANTS,
            "tools": {"named_services_call": {"grants": ["named_services:use"]}},
            "named_services": {"namespaces": {
                namespace: {"tools": {"objects": {
                    "grants": ["named_services:use", grant],
                    "operations": {name: {} for name in names},
                }}}
                for namespace, grant, names in namespaces
            }},
        })
    return connections


def _held_selection():
    return {NAMED: {"slides": SLIDES}, OTHER_NAMED: {"notes": ["object.get"]}}


async def _named_card(harness, *, selection=None):
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id=CLIENT,
        resource=NAMED,
        resource_grants={resource: GRANTS for resource in (NAMED, OTHER_NAMED)},
        resource_operations={resource: ["named_services_call"] for resource in (NAMED, OTHER_NAMED)},
        named_service_operations=_held_selection() if selection is None else selection,
        access_token="at-named",
        refresh_token="rt-named",
    )
    assert record is not None
    # Prove this fixture actually holds every operation before the demand.
    assert set(record.named_services["namespaces"]["slides"]["tools"]["objects"]["operations"]) == set(SLIDES)
    return record


async def _approve_upload(harness, record, *, claims=None, **kwargs):
    return await harness.service.extend_client_access(
        USER, client_id=CLIENT, access_id=record.access_id, resource=NAMED,
        claims=["named_services:use", "mail:send"] if claims is None else claims,
        resource_operations={NAMED: ["named_services_call"]},
        **kwargs,
    )


def _assert_extended(card):
    assert card.named_service_operations.to_stored() == {
        NAMED: {"slides": SLIDES, "mail": [UPLOAD]},
        OTHER_NAMED: {"notes": ["object.get"]},
    }
    namespaces = card.named_services["namespaces"]
    assert set(namespaces) == {"slides", "mail", "notes"}
    assert set(namespaces["slides"]["tools"]["objects"]["operations"]) == set(SLIDES)


async def _issued(harness: _Harness):
    record = await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id=CLIENT,
        client_label="Claude Code",
        scopes=["memories:read"],
        resource=CONCRETE,
        access_token="at-1",
        refresh_token="rt-1",
    )
    assert record is not None
    assert record.access_id == oauth_access_id(GRANTOR, CLIENT, CONCRETE)
    assert record.access_id != oauth_access_id(GRANTOR, CLIENT, MEMORIES)
    return record


@pytest.mark.asyncio
async def test_the_declared_door_alone_does_not_find_a_card_issued_at_a_concrete_url(tmp_path):
    harness = _Harness(tmp_path)
    await _issued(harness)

    result = await harness.service.extend_client_access(
        USER, client_id=CLIENT, resource=MEMORIES, claims=["memories:write"]
    )

    assert result["ok"] is False
    assert result["error"] == "delegated_access_unknown_client"


@pytest.mark.asyncio
async def test_the_named_card_is_extended_in_place(tmp_path):
    harness = _Harness(tmp_path)
    record = await _issued(harness)

    result = await harness.service.extend_client_access(
        USER,
        client_id=CLIENT,
        access_id=record.access_id,
        resource=MEMORIES,
        claims=["memories:write"],
    )

    assert result["ok"] is True, result
    assert list(harness.persistence.cards) == [record.access_id]
    card = harness.persistence.cards[record.access_id][0]
    assert {resource: list(grants) for resource, grants in card.resource_grants.items()} == {
        MEMORIES: ["memories:read", "memories:write"]
    }


@pytest.mark.asyncio
async def test_a_named_card_of_another_client_is_refused(tmp_path):
    harness = _Harness(tmp_path)
    record = await _issued(harness)

    result = await harness.service.extend_client_access(
        USER,
        client_id="another-client",
        access_id=record.access_id,
        resource=MEMORIES,
        claims=["memories:write"],
    )

    assert result["ok"] is False
    assert result["error"] == "delegated_access_client_mismatch"


@pytest.mark.asyncio
async def test_external_consent_adds_mail_without_losing_slides_or_another_resource(tmp_path):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)

    result = await _approve_upload(
        harness, record, named_service_operations={NAMED: {"mail": [UPLOAD]}},
    )

    assert result["ok"] is True, result
    card = await harness.card(record.access_id)
    _assert_extended(card)
    assert card.card_revision == record.card_revision + 1
    assert card.resource_grants[OTHER_NAMED] == tuple(GRANTS)


@pytest.mark.asyncio
async def test_external_extension_freezes_a_held_wildcard_before_catalog_drift(tmp_path):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness, selection="*")
    assert record.named_service_operations.is_all
    harness.catalog.publish(_named_connections(newer_catalog=True))

    result = await _approve_upload(
        harness, record, named_service_operations={NAMED: {"mail": [UPLOAD]}},
    )

    assert result["ok"] is True, result
    card = await harness.card(record.access_id)
    assert card.named_service_operations.is_exact
    selected = card.named_service_operations.to_stored()
    assert set(selected[NAMED]["slides"]) == set(SLIDES)
    assert selected[NAMED]["mail"] == [UPLOAD]
    assert selected[OTHER_NAMED] == {"notes": ["object.get"]}
    assert "object.action.delete" not in card.named_services["namespaces"]["slides"]["tools"]["objects"]["operations"]


@pytest.mark.asyncio
async def test_an_explicit_new_wildcard_can_select_the_current_catalog(tmp_path):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)
    harness.catalog.publish(_named_connections(newer_catalog=True))

    result = await _approve_upload(harness, record, named_service_operations="*")

    assert result["ok"] is True, result
    card = await harness.card(record.access_id)
    assert card.named_service_operations.is_all
    assert "object.action.delete" in card.named_services["namespaces"]["slides"]["tools"]["objects"]["operations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, {}], ids=["omitted", "empty-delta"])
async def test_external_merge_without_an_added_selection_preserves_held_operations(tmp_path, selection):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)

    result = await _approve_upload(harness, record, named_service_operations=selection)

    assert result["ok"] is True, result
    assert (await harness.card(record.access_id)).named_service_operations.to_stored() == _held_selection()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, {}, {NAMED: {"mail": [UPLOAD]}}], ids=["omitted", "clear", "replace"])
async def test_external_replace_preserves_omission_and_replaces_explicit_selection(tmp_path, selection):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)

    # This test changes only the named-service dimension; replacement claims
    # must continue to permit the operations whose selection was omitted.
    result = await _approve_upload(harness, record, claims=GRANTS, named_service_operations=selection, replace=True)

    assert result["ok"] is True, result
    expected = _held_selection() if selection is None else selection
    assert (await harness.card(record.access_id)).named_service_operations.to_stored() == expected


@pytest.mark.asyncio
async def test_external_merge_rejects_malformed_selection_without_writing(tmp_path):
    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)
    writes = harness.persistence.persist_calls

    result = await _approve_upload(harness, record, named_service_operations={NAMED: []})

    assert result["error"] == "invalid_named_service_operation_selection"
    assert harness.persistence.persist_calls == writes
    assert (await harness.card(record.access_id)).card_revision == record.card_revision


@pytest.mark.asyncio
async def test_hosted_agent_consent_still_unions_the_held_named_selection(tmp_path):
    harness = _Harness(tmp_path, connections=_named_connections())
    client = "kdcube-agent:workspace@1-0:named"
    created = await harness.service.create_access(
        USER, label="Named agent", client_id=client, resource_grants={NAMED: GRANTS},
        named_service_operations={NAMED: {"slides": SLIDES}},
    )
    assert created["ok"] is True, created

    result = await harness.service.create_access(
        USER, label="Named agent", client_id=client, resource_grants={NAMED: ["named_services:use", "mail:send"]},
        named_service_operations={NAMED: {"mail": [UPLOAD]}}, merge_existing=True,
    )

    assert result["ok"] is True, result
    assert result["access_id"] == created["access_id"]
    assert result["access"]["named_service_operations"] == {NAMED: {"mail": [UPLOAD], "slides": SLIDES}}

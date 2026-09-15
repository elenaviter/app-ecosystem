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
from test_resident_profile_cards import GRANTOR, MEMORIES, USER, _Harness

CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"
CONCRETE = "https://host/api/mcp/memories"


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

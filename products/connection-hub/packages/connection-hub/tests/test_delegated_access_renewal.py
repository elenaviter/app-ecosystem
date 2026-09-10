# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An expired manual card keeps its grants and is renewed in place: it stays
listed, renewal mints a new bearer on the same card and access_id, the old
session ends, and the other card families are pointed to their own way back."""

from __future__ import annotations

import dataclasses
import time

import pytest

from connection_hub.delegated_credentials.automation_access import ACCESS_SOURCE_OAUTH
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.service import replace_state
from test_resident_profile_cards import GRANTOR, MEMORIES, OTHER_USER, USER, _Harness


async def _manual_card(harness: _Harness, *, ttl: int = 3600) -> dict:
    created = await harness.service.create_access(
        USER,
        label="Steuer",
        resource_grants={MEMORIES: ["memories:read", "memories:write"]},
        resource_operations={MEMORIES: ["search", "write"]},
        ttl_seconds=ttl,
    )
    assert created["ok"] is True, created
    return created


def _expire(harness: _Harness, access_id: str, *, ago: int = 60) -> None:
    authority, handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, expires_at=int(time.time()) - ago),
        handles,
    )


@pytest.mark.asyncio
async def test_an_expired_card_stays_listed_and_says_so(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]
    _expire(harness, access_id)

    listed = await harness.service.list_access(USER)
    assert listed["ok"] is True
    items = {item["access_id"]: item for item in listed["items"]}
    assert access_id in items, "an expired card must not vanish from its owner's list"
    assert items[access_id]["expired"] is True
    assert items[access_id]["resource_grants"][MEMORIES] == ["memories:read", "memories:write"]


@pytest.mark.asyncio
async def test_renewal_keeps_the_card_and_mints_a_new_bearer(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]
    old_token = created["access_token"]
    old_revision = created["access"]["card_revision"]
    old_session = harness.persistence.cards[access_id][1].session_id
    _expire(harness, access_id)

    renewed = await harness.service.renew_access(USER, access_id=access_id)
    assert renewed["ok"] is True, renewed
    access = renewed["access"]
    assert access["access_id"] == access_id
    assert access["card_revision"] == old_revision + 1
    assert renewed["access_token"] and renewed["access_token"] != old_token
    assert renewed["authorization_header"] == f"Bearer {renewed['access_token']}"
    now = int(time.time())
    # Default lifetime is the card's previous one (one hour here).
    assert now + 3600 - 5 <= access["expires_at"] <= now + 3600 + 5
    assert access["last_issued_at"] >= now - 5
    assert access["last_four"] == renewed["access_token"][-4:]
    # The work survives untouched.
    assert access["resource_grants"][MEMORIES] == ["memories:read", "memories:write"]
    assert sorted(access["resource_operations"][MEMORIES]) == ["search", "write"]
    assert access["provenance"]["renewals"] == 1
    # The new bearer is bound to the same card; the old session is over.
    assert harness.grant_store.bindings[renewed["access_token"]]["registry_access_id"] == access_id
    assert old_session in harness.authority.logged_out

    listed = await harness.service.list_access(USER)
    item = next(item for item in listed["items"] if item["access_id"] == access_id)
    assert item["expired"] is False


@pytest.mark.asyncio
async def test_renewal_before_expiry_rotates_and_honours_a_requested_lifetime(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness, ttl=3600)
    access_id = created["access"]["access_id"]

    renewed = await harness.service.renew_access(USER, access_id=access_id, ttl_seconds=7200)
    assert renewed["ok"] is True, renewed
    now = int(time.time())
    assert now + 7200 - 5 <= renewed["access"]["expires_at"] <= now + 7200 + 5
    assert renewed["access_token"] != created["access_token"]


@pytest.mark.asyncio
async def test_renewal_refusals(tmp_path):
    harness = _Harness(tmp_path)
    created = await _manual_card(harness)
    access_id = created["access"]["access_id"]

    # Another user cannot see it, let alone renew it.
    other = await harness.service.renew_access(OTHER_USER, access_id=access_id)
    assert other["ok"] is False and other["error"] == "delegated_access_not_found"

    # A connected app renews by reconnecting from the client.
    authority, handles = harness.persistence.cards[access_id]
    harness.persistence.cards[access_id] = (
        dataclasses.replace(authority, source=ACCESS_SOURCE_OAUTH), handles,
    )
    oauth = await harness.service.renew_access(USER, access_id=access_id)
    assert oauth["ok"] is False and oauth["error"] == "delegated_access_renew_unsupported"
    assert oauth["source"] == ACCESS_SOURCE_OAUTH
    harness.persistence.cards[access_id] = (authority, handles)

    # A revoked card is not revived by renewal.
    harness.persistence.cards[access_id] = (replace_state(authority, CARD_STATE_REVOKED), handles)
    revoked = await harness.service.renew_access(USER, access_id=access_id)
    assert revoked["ok"] is False and revoked["error"] == "delegated_access_revoked"

    unknown = await harness.service.renew_access(USER, access_id="missing")
    assert unknown["ok"] is False and unknown["error"] == "delegated_access_not_found"
    assert GRANTOR == USER["user_id"]

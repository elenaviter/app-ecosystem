# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W918: a delegated caller, such as a Problem Board agent with its Card's bearer, never approves for itself.

An agent reaches the services its Card allows through Connection Hub's gateway with its Card's own bearer.
Granting a service, choosing Once or Every time, and changing the Card stay with the Card's owner in a
human session. A delegated caller authenticates as integration:<client>:<grantor>: it owns no Card to
approve on, and every Card change refuses it with delegated_access_requires_grantor.
"""

from __future__ import annotations

import pytest

from test_resident_profile_cards import GRANTOR, MEMORIES, USER, _Harness

DELEGATED = {"user_id": f"integration:dcr-agent:{GRANTOR}", "roles": ["kdcube:role:registered"], "permissions": []}


async def _owner_card(harness: _Harness) -> str:
    created = await harness.service.create_access(USER, label="agent", resource_grants={MEMORIES: ["memories:read"]})
    assert created.get("ok") is not False, created
    return next(iter(harness.persistence.cards))


@pytest.mark.asyncio
async def test_a_delegated_caller_owns_no_card_to_approve_on(tmp_path):
    harness = _Harness(tmp_path)
    await _owner_card(harness)

    listing = await harness.service.list_access(DELEGATED)

    assert listing.get("items", []) == [], listing


@pytest.mark.asyncio
async def test_a_delegated_caller_cannot_grant_or_change_a_card(tmp_path):
    harness = _Harness(tmp_path)
    access_id = await _owner_card(harness)
    before = harness.persistence.cards[access_id]

    attempts = {
        "create": await harness.service.create_access(
            DELEGATED, label="self", resource_grants={MEMORIES: ["memories:write"]}),
        "extend": await harness.service.extend_client_access(
            DELEGATED, client_id="dcr-agent", access_id=access_id, resource=MEMORIES, claims=["memories:write"]),
        "revoke": await harness.service.revoke_access(DELEGATED, access_id=access_id),
    }

    for name, result in attempts.items():
        assert result["ok"] is False, (name, result)
        assert result["error"] == "delegated_access_requires_grantor", (name, result)
    assert harness.persistence.cards[access_id] == before, "no Card revision was written"

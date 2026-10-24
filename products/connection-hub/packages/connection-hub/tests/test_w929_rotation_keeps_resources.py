# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W929: a refresh rotation keeps an original-exchange family's resources.

An original-exchange family (W676) names its plan's Card selectors as ``resources``; the refresh reads
them to find what the Card still covers. The rotation rebuilt the record from fixed fields and dropped
them, so a family with several selectors narrowed after its first refresh.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from connection_hub.delegated_credentials.oauth.store import GrantStore

SELECTORS = ["urn:kdcube:management:deployment:*:*", "urn:kdcube:management:secret:*:*:*:*:*"]


def _rotate_twice(record):
    replacements = []

    async def rotate(token, replacement, **kwargs):
        replacements.append(replacement)
        return f"successor-{len(replacements)}"

    async def invoke():
        store = GrantStore(None, "test", "test", authority_store=SimpleNamespace(rotate_refresh_token=rotate))
        current = record
        for generation in (1, 2):
            state = SimpleNamespace(token=f"t{generation}", raw=f"g{generation}", record=current)
            assert await store.rotate_refresh_token(f"t{generation}", state=state) == f"successor-{generation}"
            current = replacements[-1]

    asyncio.run(invoke())
    return replacements


def test_two_refreshes_keep_both_selectors():
    record = {"client_id": "cli", "sub": "owner", "scopes": ["s"], "card_kind": "connector",
              "registry_access_id": "card-1", "resources": list(SELECTORS)}

    first, second = _rotate_twice(record)

    assert first["resources"] == SELECTORS
    assert second["resources"] == SELECTORS


def test_a_family_without_resources_gains_none():
    record = {"client_id": "cli", "sub": "owner", "scopes": ["s"], "card_kind": "connector",
              "registry_access_id": "card-1", "resource": "urn:kdcube:management:deployment:t:p"}

    first, second = _rotate_twice(record)

    assert "resources" not in first and "resources" not in second
    assert second["resource"] == "urn:kdcube:management:deployment:t:p"

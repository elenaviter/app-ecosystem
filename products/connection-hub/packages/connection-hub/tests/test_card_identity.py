# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Stable resident caller identity: one Card per grantor, app, and agent."""

from __future__ import annotations

import hashlib

import pytest

from connection_hub.delegated_credentials.automation_access import (
    agent_grant_access_id,
    oauth_access_id,
    resident_access_ids,
)
from connection_hub.delegated_credentials.cards.identity import (
    RESIDENT_ACCESS_ID_PREFIX,
    ResidentCallerProfile,
    is_resident_client_id,
    legacy_resident_access_id,
    resident_client_id,
    stable_resident_access_id,
)

GRANTOR = "platform-user-1"
CLIENT = "kdcube-agent:workspace@1-0:lg-react"
MEMORIES = "https://host/api/mcp/memories*"
TASKS = "https://host/api/mcp/tasks*"


def test_stable_id_does_not_depend_on_resources():
    profile = ResidentCallerProfile.parse(GRANTOR, CLIENT)
    assert profile is not None
    assert profile.access_id == stable_resident_access_id(GRANTOR, CLIENT)
    # The id is a property of the profile alone; there is no resource input.
    assert profile.access_id.startswith(RESIDENT_ACCESS_ID_PREFIX)
    assert len(profile.access_id) == len(RESIDENT_ACCESS_ID_PREFIX) + 16
    # The legacy formula changed with every resource set; the stable one never
    # collides with any of them, including the empty set.
    legacy_ids = {
        legacy_resident_access_id(GRANTOR, CLIENT, resources)
        for resources in ([], [MEMORIES], [TASKS], [MEMORIES, TASKS])
    }
    assert len(legacy_ids) == 4
    assert profile.access_id not in legacy_ids


def test_grantor_application_and_agent_each_separate_the_identity():
    base = stable_resident_access_id(GRANTOR, CLIENT)
    assert stable_resident_access_id("platform-user-2", CLIENT) != base
    assert stable_resident_access_id(GRANTOR, "kdcube-agent:other-app@1-0:lg-react") != base
    assert stable_resident_access_id(GRANTOR, "kdcube-agent:workspace@1-0:other-agent") != base
    # Two agents of one app, one agent in two apps, one agent for two grantors:
    # four profiles, four cards.
    ids = {
        ResidentCallerProfile(grantor_subject=g, application=a, agent_id=ag).access_id
        for g, a, ag in (
            (GRANTOR, "workspace@1-0", "lg-react"),
            (GRANTOR, "workspace@1-0", "planner"),
            (GRANTOR, "assistant@1-0", "lg-react"),
            ("platform-user-2", "workspace@1-0", "lg-react"),
        )
    }
    assert len(ids) == 4


def test_identity_scope_is_card_content_not_resident_identity():
    profile = ResidentCallerProfile.parse(GRANTOR, CLIENT)
    assert profile is not None
    assert set(profile.to_dict()) == {
        "kind",
        "grantor_subject",
        "application",
        "agent_id",
        "client_id",
        "access_id",
    }


def test_profile_parses_only_resident_client_ids():
    assert ResidentCallerProfile.parse(GRANTOR, "dcr-7c1e2b9a") is None
    assert ResidentCallerProfile.parse(GRANTOR, "automation:abc") is None
    assert ResidentCallerProfile.parse(GRANTOR, "kdcube-agent:only-app") is None
    assert ResidentCallerProfile.parse("", CLIENT) is None
    profile = ResidentCallerProfile.parse(GRANTOR, CLIENT)
    assert (profile.application, profile.agent_id) == ("workspace@1-0", "lg-react")
    assert profile.client_id == CLIENT
    assert resident_client_id("workspace@1-0", "lg-react") == CLIENT
    assert is_resident_client_id(CLIENT) and not is_resident_client_id("dcr-1")
    with pytest.raises(ValueError):
        ResidentCallerProfile(grantor_subject=GRANTOR, application="", agent_id="x")


def test_legacy_formula_is_preserved_for_migration_lookups():
    expected = "agent-" + hashlib.sha256(
        f"{GRANTOR}|{CLIENT}|{MEMORIES}+{TASKS}".encode("utf-8")
    ).hexdigest()[:16]
    assert legacy_resident_access_id(GRANTOR, CLIENT, [TASKS, MEMORIES]) == expected
    assert agent_grant_access_id(GRANTOR, CLIENT, [TASKS, MEMORIES]) == expected
    # Lookup order: the one stable id, then the legacy record.
    order = resident_access_ids(GRANTOR, CLIENT, [MEMORIES])
    assert order[0] == stable_resident_access_id(GRANTOR, CLIENT)
    assert order[-1] == legacy_resident_access_id(GRANTOR, CLIENT, [MEMORIES])
    assert len(order) == len(set(order))
    assert resident_access_ids(GRANTOR, "dcr-7c1e2b9a", [MEMORIES]) == []


def test_oauth_connections_with_equal_public_metadata_stay_independent():
    """Two dynamic clients registered by the same public app (Claude) receive
    distinct client ids; their cards must not collapse, and nothing about them
    follows the resident rule."""
    a = oauth_access_id(GRANTOR, "dcr-aaaa1111", MEMORIES)
    b = oauth_access_id(GRANTOR, "dcr-bbbb2222", MEMORIES)
    assert a != b
    # The same public client id is one connection per grantor and resource.
    assert oauth_access_id(GRANTOR, "claude", MEMORIES) == oauth_access_id(GRANTOR, "claude", MEMORIES)
    assert oauth_access_id(GRANTOR, "claude", MEMORIES) != oauth_access_id(GRANTOR, "claude", TASKS)
    assert ResidentCallerProfile.parse(GRANTOR, "dcr-aaaa1111") is None

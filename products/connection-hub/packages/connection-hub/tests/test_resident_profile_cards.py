# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Multi-resource cards on one stable resident profile: create, merge, replace,
remove, identity-scope separation, legacy fold, policy survival, cross-owner
isolation, and per-resource acceptance through the service."""

from __future__ import annotations

import asyncio
import itertools
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.automation_access import (
    RESIDENT_MIGRATION_CONFLICT,
    ACCESS_SOURCE_AGENT,
    AutomationAccessService,
    agent_grant_access_id,
)
from connection_hub.delegated_credentials.cards.identity import (
    ResidentCallerProfile,
    stable_resident_access_id,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_REVOKED,
    CardAuthority,
    CardCredentialHandles,
    NamedServiceSelection,
    authority_is_usable,
)
from connection_hub.delegated_credentials.cards.service import CardConflict, replace_state
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.invocation_policy import (
    POLICY_ALWAYS,
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
    canonical_request_digest,
)

GRANTOR = "user-1"
OTHER = "user-2"
CLIENT = "kdcube-agent:workspace@1-0:lg-react"
MEMORIES = "https://host/api/mcp/memories*"
TASKS = "https://host/api/mcp/tasks*"
MAIL = "https://host/api/mcp/mail*"
USER = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"], "permissions": []}
OTHER_USER = {"user_id": OTHER, "roles": ["kdcube:role:registered"], "permissions": []}


def _connections(*, tasks_delete_description="Delete a task"):
    return {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": g, "label": g, "delegable_roles": ["kdcube:role:registered"]}
                    for g in ("memories:read", "memories:write", "tasks:use", "mail:read")
                ],
                "resources": [
                    {
                        "resource": MEMORIES,
                        "label": "Memories",
                        "grants": ["memories:read", "memories:write"],
                        "tools": {
                            "search": {"grants": ["memories:read"], "description": "Search memories"},
                            "write": {"grants": ["memories:write"], "description": "Write a memory"},
                        },
                    },
                    {
                        "resource": TASKS,
                        "label": "Tasks",
                        "grants": ["tasks:use"],
                        "tools": {
                            "search": {"grants": ["tasks:use"], "description": "Search tasks"},
                            "delete": {"grants": ["tasks:use"], "description": tasks_delete_description},
                        },
                    },
                    {
                        "resource": MAIL,
                        "label": "Mail",
                        "grants": ["mail:read"],
                        "identity_scope": "family",
                        "tools": {"read": {"grants": ["mail:read"], "description": "Read mail"}},
                    },
                ],
            }
        }
    }


class _Catalog:
    def __init__(self, connections):
        self.docs: dict[str, CatalogDocument] = {}
        self._stamp = itertools.count(1_780_000_000)
        self.publish(connections)

    def publish(self, connections) -> CatalogDocument:
        document = CatalogDocument.build(
            connections, created_at=datetime.fromtimestamp(next(self._stamp), tz=timezone.utc)
        )
        self.docs[document.version] = document
        self.active = document
        return document

    async def resolve_active(self):
        return self.active

    async def resolve_version(self, version):
        return self.docs.get(version)


class _Persistence:
    """In-memory card persistence with the durable port's semantics."""

    def __init__(self) -> None:
        self.cards: dict[str, tuple[CardAuthority, CardCredentialHandles]] = {}
        self.persist_calls = 0

    def _owned(self, authority: CardAuthority, subject_hash: str) -> bool:
        return subject_hash_for(authority.grantor_subject) == subject_hash

    async def load(self, access_id, *, subject_hash):
        entry = self.cards.get(access_id)
        if entry is None or not self._owned(entry[0], subject_hash):
            return None
        if not authority_is_usable(entry[0], int(time.time())):
            return None
        return entry

    async def current_revision(self, access_id, *, subject_hash):
        entry = self.cards.get(access_id)
        return entry[0].card_revision if entry is not None else 0

    async def persist(self, authority, handles, *, subject_hash, expected_revision):
        current = await self.current_revision(authority.access_id, subject_hash=subject_hash)
        if current != int(expected_revision):
            raise CardConflict("card_revision_moved", current_revision=current)
        self.persist_calls += 1
        self.cards[authority.access_id] = (authority, handles)

    async def forget(self, authority, *, subject_hash):
        self.cards[authority.access_id] = (
            replace_state(authority, CARD_STATE_REVOKED),
            CardCredentialHandles(access_id=authority.access_id),
        )

    async def list_active(self, *, subject_hash, now=None):
        moment = int(now if now is not None else time.time())
        return [
            authority
            for authority, _ in self.cards.values()
            if self._owned(authority, subject_hash) and authority_is_usable(authority, moment)
        ]

    def seed(self, authority: CardAuthority, *, access_token: str = "") -> None:
        self.cards[authority.access_id] = (
            authority, CardCredentialHandles(access_id=authority.access_id, access_token=access_token)
        )


class _GrantStore:
    refresh_ttl = 86400

    def __init__(self) -> None:
        self.bindings: dict[str, dict] = {}
        self.revoked: list[str] = []

    async def bind_access_grant(self, token, operations, expires_in, **kwargs):
        self.bindings[token] = {"operations": list(operations), **kwargs}

    async def revoke_access_grant(self, token):
        self.revoked.append(token)
        self.bindings.pop(token, None)

    async def revoke_refresh_token(self, token):
        return True


class _Authority:
    def __init__(self) -> None:
        self.logged_out: list[str] = []

    async def logout(self, *, session_id):
        self.logged_out.append(session_id)
        return True


class _Locks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def __call__(self, *, lock_path, **_kwargs):
        lock = self._locks.setdefault(str(lock_path), asyncio.Lock())
        async with lock:
            yield {}


class _Harness:
    def __init__(self, tmp_path, *, policies: bool = True) -> None:
        self.connections = _connections()
        self.catalog = _Catalog(self.connections)
        self.persistence = _Persistence()
        self.grant_store = _GrantStore()
        self.authority = _Authority()
        self.tokens = itertools.count(1)
        self.policies = (
            InvocationPolicyService(
                store=BundleStorageInvocationPolicyStore(tmp_path / "policies"),
                mutation_lock=_Locks(),
            )
            if policies
            else None
        )

        async def minter(grantor, grants, *, authority, client_id, operations, credential, ttl_seconds):
            number = next(self.tokens)
            return {"access_token": f"tok-{number}", "expires_in": ttl_seconds, "session_id": f"sess-{number}"}

        self.service = AutomationAccessService(
            redis=object(),
            tenant="tenant-a",
            project="project-a",
            config=oauth_delegated_config_from_connections(self.connections),
            grant_store=self.grant_store,
            authority=self.authority,
            catalog_resolver=self.catalog,
            card_persistence=self.persistence,
            minter=minter,
            invocation_policy_service=self.policies,
        )

    async def card(self, access_id: str, *, grantor: str = GRANTOR):
        loaded = await self.persistence.load(access_id, subject_hash=subject_hash_for(grantor))
        return loaded[0] if loaded is not None else None

    def legacy_authority(
        self,
        *,
        resource: str,
        grants: tuple[str, ...],
        operations: tuple[str, ...],
        created_at: int,
        expires_at: int,
        account_scope=None,
        identity_scope="grantor",
    ) -> CardAuthority:
        return CardAuthority(
            access_id=agent_grant_access_id(GRANTOR, CLIENT, [resource]),
            client_id=CLIENT,
            grantor_subject=GRANTOR,
            delegate_subject=f"integration:{CLIENT}:{GRANTOR}",
            source=ACCESS_SOURCE_AGENT,
            label="lg-react",
            card_revision=2,
            catalog_version=self.catalog.active.version,
            resource_grants={resource: grants},
            resource_operations={resource: operations},
            named_service_operations=NamedServiceSelection.none(),
            account_scope=account_scope or {},
            identity_scope=identity_scope,
            created_at=created_at,
            expires_at=expires_at,
        )


def _profile() -> ResidentCallerProfile:
    return ResidentCallerProfile.parse(GRANTOR, CLIENT)


@pytest.mark.asyncio
async def test_one_stable_card_survives_adding_and_removing_resources(tmp_path):
    h = _Harness(tmp_path)
    first = await h.service.create_access(
        USER,
        label="lg-react",
        resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]},
        client_id=CLIENT,
    )
    assert first["ok"], first
    access_id = first["access"]["access_id"]
    assert access_id == _profile().access_id
    assert access_id == stable_resident_access_id(GRANTOR, CLIENT)
    created_at = first["access"]["created_at"]

    # Incremental consent merges one exact resource/operation into the SAME card.
    second = await h.service.create_access(
        USER,
        label="",
        resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["delete"]},
        client_id=CLIENT,
    )
    assert second["ok"], second
    assert second["access"]["access_id"] == access_id
    assert second["access"]["resource_grants"] == {MEMORIES: ["memories:read"], TASKS: ["tasks:use"]}
    assert second["access"]["resource_operations"] == {MEMORIES: ["search"], TASKS: ["delete"]}
    assert set(second["access"]["resource_acceptance"]) == {MEMORIES, TASKS}
    assert second["access"]["created_at"] == created_at
    assert len([a for a, _ in h.persistence.cards.values() if a.client_id == CLIENT]) == 1

    # The per-turn resolver finds the bearer for either resource on one card.
    for resource in (MEMORIES, TASKS):
        token = await h.service.agent_access_token(
            grantor_subject=GRANTOR, client_id=CLIENT, resources=[resource]
        )
        assert token is not None and token["access_token"] == second["access_token"]
    assert await h.service.agent_access_token(
        grantor_subject=GRANTOR, client_id=CLIENT, resources=[MAIL]
    ) is None

    # An ordinary edit replaces the card's contents: dropping memories keeps
    # tasks, its acceptance, the id, the credential, and creation provenance.
    removed = await h.service.update_access(
        USER,
        access_id=access_id,
        resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["delete"]},
    )
    assert removed["ok"], removed
    card = await h.card(access_id)
    assert card.access_id == access_id
    assert dict(card.resource_grants) == {TASKS: ("tasks:use",)}
    assert dict(card.resource_operations) == {TASKS: ("delete",)}
    assert set(card.resource_acceptance) == {TASKS}
    assert card.created_at == created_at
    handles = h.persistence.cards[access_id][1]
    assert handles.access_token == second["access_token"]
    assert await h.service.agent_access_token(
        grantor_subject=GRANTOR, client_id=CLIENT, resources=[MEMORIES]
    ) is None

    # Re-adding the resource lands on the same card again.
    third = await h.service.create_access(
        USER,
        label="",
        resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]},
        client_id=CLIENT,
    )
    assert third["ok"] and third["access"]["access_id"] == access_id
    assert set(third["access"]["resource_grants"]) == {MEMORIES, TASKS}


@pytest.mark.asyncio
async def test_removing_the_final_resource_is_refused_and_revocation_is_the_way_out(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER, label="lg-react", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search"]}, client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]
    refused = await h.service.update_access(USER, access_id=access_id, resource_grants={})
    assert refused == {"ok": False, "error": "delegated_access_requires_resource_grants"}
    assert (await h.card(access_id)).card_revision == created["access"]["card_revision"]
    revoked = await h.service.revoke_access(USER, access_id=access_id)
    assert revoked["ok"] and revoked["removed"]
    assert await h.card(access_id) is None
    assert created["access_token"] in h.grant_store.revoked


@pytest.mark.asyncio
async def test_incompatible_identity_scope_never_creates_a_second_resident_card(tmp_path):
    h = _Harness(tmp_path)
    mixed = await h.service.create_access(
        USER, label="lg-react",
        resource_grants={MEMORIES: ["memories:read"], MAIL: ["mail:read"]},
        client_id=CLIENT,
    )
    assert mixed["error"] == "delegated_access_resources_have_conflicting_identity_scopes"
    assert h.persistence.persist_calls == 0

    memories = await h.service.create_access(
        USER, label="lg-react", resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]}, client_id=CLIENT,
    )
    mail = await h.service.create_access(
        USER, label="lg-react", resource_grants={MAIL: ["mail:read"]},
        resource_operations={MAIL: ["read"]}, client_id=CLIENT,
    )
    assert memories["ok"]
    assert mail["ok"] is False
    assert mail["error"] == "delegated_access_resources_have_conflicting_identity_scopes"
    card = await h.card(memories["access"]["access_id"])
    assert card.resource_grants == {MEMORIES: ("memories:read",)}

    edited = await h.service.update_access(
        USER,
        access_id=memories["access"]["access_id"],
        resource_grants={MEMORIES: ["memories:read"], MAIL: ["mail:read"]},
        resource_operations={MEMORIES: ["search"], MAIL: ["read"]},
    )
    assert edited["ok"] is False
    assert edited["error"] == "delegated_access_resources_have_conflicting_identity_scopes"
    assert "existing card is unchanged" in edited["message"]
    assert len([a for a, _ in h.persistence.cards.values() if a.client_id == CLIENT]) == 1
    card = await h.card(memories["access"]["access_id"])
    assert card.resource_grants == {MEMORIES: ("memories:read",)}
    assert await h.service.agent_access_token(
        grantor_subject=GRANTOR, client_id=CLIENT, resources=[MAIL]
    ) is None


@pytest.mark.asyncio
async def test_equal_operation_names_on_two_resources_stay_qualified(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER, label="lg-react",
        resource_grants={MEMORIES: ["memories:read"], TASKS: ["tasks:use"]},
        resource_operations={MEMORIES: ["search"], TASKS: ["search"]},
        client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]
    await h.policies.set_policy(
        owner_subject=GRANTOR,
        authority=InvocationAuthority(access_id=access_id, resource=TASKS, surface=SURFACE_OUTER, operation="search"),
        mode=POLICY_ONCE,
    )
    described = await h.service.describe_card(USER, access_id=access_id)
    assert described["ok"], described
    by_resource = {entry["resource"]: entry for entry in described["card"]["resources"]}
    assert by_resource[TASKS]["operations"][0]["policy"]["mode"] == "once"
    assert "policy" not in by_resource[MEMORIES]["operations"][0]
    # Narrowing memories' search leaves tasks' search and its policy alone.
    narrowed = await h.service.update_access(
        USER, access_id=access_id,
        resource_grants={MEMORIES: ["memories:read"], TASKS: ["tasks:use"]},
        resource_operations={MEMORIES: [], TASKS: ["search"]},
    )
    assert narrowed["ok"] and narrowed["access"]["resource_operations"] == {MEMORIES: [], TASKS: ["search"]}
    view = await h.service.resident_profile_card(grantor_subject=GRANTOR, client_id=CLIENT)
    assert view.resource(TASKS).operations[0].policy["mode"] == "once"


@pytest.mark.asyncio
async def test_exact_access_id_card_reader_is_owner_scoped_and_typed(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER,
        label="lg-react",
        resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]},
        client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]

    card = await h.service.card_for_access_id(
        grantor_subject=GRANTOR,
        access_id=access_id,
    )

    assert card is not None
    assert card.access_id == access_id
    assert card.resource(MEMORIES).operations[0].name == "search"
    assert await h.service.card_for_access_id(
        grantor_subject=OTHER,
        access_id=access_id,
    ) is None


@pytest.mark.asyncio
async def test_resident_profile_exact_token_lookup_returns_card_observation(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER,
        label="lg-react",
        resource_grants={MEMORIES: ["memories:read"], TASKS: ["tasks:use"]},
        resource_operations={MEMORIES: ["search"], TASKS: ["delete"]},
        client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]

    exact = await h.service.resident_agent_access_token_for_access_id(
        grantor_subject=GRANTOR,
        client_id=CLIENT,
        access_id=access_id,
    )

    assert exact["access_token"] == created["access_token"]
    assert exact["access_id"] == access_id
    assert exact["card_revision"] == created["access"]["card_revision"]
    assert exact["card"]["access_id"] == access_id
    assert {row["resource"] for row in exact["card"]["resources"]} == {
        MEMORIES,
        TASKS,
    }
    assert await h.service.resident_agent_access_token_for_access_id(
        grantor_subject=OTHER,
        client_id=CLIENT,
        access_id=access_id,
    ) is None
    assert await h.service.resident_agent_access_token_for_access_id(
        grantor_subject=GRANTOR,
        client_id="kdcube-agent:workspace@1-0:other",
        access_id=access_id,
    ) is None
    assert "access_token" not in str(exact["card"])


@pytest.mark.asyncio
async def test_legacy_resident_cards_fold_once_without_widening(tmp_path):
    h = _Harness(tmp_path)
    now = int(time.time())
    legacy_a = h.legacy_authority(
        resource=MEMORIES, grants=("memories:read",), operations=("search",),
        created_at=now - 5000, expires_at=now + 40_000,
    )
    legacy_b = h.legacy_authority(
        resource=TASKS, grants=("tasks:use",), operations=("search", "delete"),
        created_at=now - 3000, expires_at=now + 20_000,
    )
    h.persistence.seed(legacy_a, access_token="old-a")
    h.persistence.seed(legacy_b, access_token="old-b")
    a_search = InvocationAuthority(access_id=legacy_a.access_id, resource=MEMORIES, surface=SURFACE_OUTER, operation="search")
    b_delete = InvocationAuthority(access_id=legacy_b.access_id, resource=TASKS, surface=SURFACE_OUTER, operation="delete")
    await h.policies.set_policy(owner_subject=GRANTOR, authority=a_search, mode=POLICY_ALWAYS)
    await h.policies.set_policy(owner_subject=GRANTOR, authority=b_delete, mode=POLICY_ONCE)
    consumed = await h.policies.begin(
        owner_subject=GRANTOR, authority=b_delete, invocation_id="inv-1",
        request_digest=canonical_request_digest({"id": "t-1"}), card_revision=2,
    )
    assert consumed.dispatch is True and consumed.policy.remaining == 0

    # Before the fold, reads still reach the legacy records.
    legacy_token = await h.service.agent_access_token(
        grantor_subject=GRANTOR, client_id=CLIENT, resources=[TASKS]
    )
    assert legacy_token["access_token"] == "old-b"

    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    assert result["ok"], result
    stable_id = _profile().access_id
    assert result["access_id"] == stable_id
    assert sorted(result["folded"]) == sorted([legacy_a.access_id, legacy_b.access_id])
    assert result["dropped_consumed_once"] == [{"resource": TASKS, "operation": "delete"}]
    assert result["expires_at"] == now + 20_000

    card = await h.card(stable_id)
    assert dict(card.resource_grants) == {MEMORIES: ("memories:read",), TASKS: ("tasks:use",)}
    # The spent one-use permit did not come back: `delete` is not on the card.
    assert dict(card.resource_operations) == {MEMORIES: ("search",), TASKS: ("search",)}
    assert card.expires_at == now + 20_000
    assert card.created_at == now - 5000
    lineage = card.provenance["migrated_from"]
    assert {item["access_id"] for item in lineage} == {legacy_a.access_id, legacy_b.access_id}
    assert card.provenance["dropped_consumed_once"] == [{"resource": TASKS, "operation": "delete"}]
    assert set(card.resource_acceptance) == {MEMORIES, TASKS}
    # Policies moved with their operation: `always` re-declared on the stable card.
    moved = await h.policies.get(
        owner_subject=GRANTOR,
        authority=InvocationAuthority(access_id=stable_id, resource=MEMORIES, surface=SURFACE_OUTER, operation="search"),
    )
    assert moved is not None and moved.mode == POLICY_ALWAYS
    assert await h.policies.get(
        owner_subject=GRANTOR,
        authority=InvocationAuthority(access_id=stable_id, resource=TASKS, surface=SURFACE_OUTER, operation="delete"),
    ) is None
    # Legacy cards are revoked and their bearers invalidated; the stable card
    # holds a fresh bearer that the per-turn resolver now returns.
    assert await h.card(legacy_a.access_id) is None and await h.card(legacy_b.access_id) is None
    assert {"old-a", "old-b"} <= set(h.grant_store.revoked)
    fresh = await h.service.agent_access_token(grantor_subject=GRANTOR, client_id=CLIENT, resources=[TASKS])
    assert fresh["access_token"] == h.persistence.cards[stable_id][1].access_token
    assert fresh["access_token"] not in {"old-a", "old-b"}
    assert h.grant_store.bindings[fresh["access_token"]]["registry_access_id"] == stable_id

    # Replay is a no-op, and the next consent merges into the stable card.
    again = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    assert again == {"ok": True, "access_id": stable_id, "folded": [], "noop": True}
    consent = await h.service.create_access(
        USER, label="", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["delete"]}, client_id=CLIENT,
    )
    assert consent["ok"] and consent["access"]["access_id"] == stable_id
    assert consent["access"]["resource_operations"][TASKS] == ["delete", "search"]


@pytest.mark.asyncio
async def test_fold_refuses_disagreeing_account_bindings_and_changes_nothing(tmp_path):
    h = _Harness(tmp_path)
    now = int(time.time())
    legacy_a = h.legacy_authority(
        resource=MEMORIES, grants=("memories:read",), operations=("search",),
        created_at=now - 100, expires_at=now + 1000,
        account_scope={"slack": {"ws-1": ("slack:post",)}},
    )
    legacy_b = h.legacy_authority(
        resource=TASKS, grants=("tasks:use",), operations=("search",),
        created_at=now - 50, expires_at=now + 1000,
        account_scope={"slack": {"ws-2": ("slack:post",)}},
    )
    h.persistence.seed(legacy_a, access_token="old-a")
    h.persistence.seed(legacy_b, access_token="old-b")
    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    assert result["ok"] is False
    assert result["error"] == RESIDENT_MIGRATION_CONFLICT
    assert result["reason"] == "account_scope_conflict"
    assert {item["access_id"] for item in result["candidates"]} == {legacy_a.access_id, legacy_b.access_id}
    assert await h.card(_profile().access_id) is None
    assert await h.card(legacy_a.access_id) is not None and await h.card(legacy_b.access_id) is not None
    assert h.grant_store.revoked == [] and h.persistence.persist_calls == 0
    # A consent grant for this profile reports the same conflict instead of
    # writing a stable card beside the disagreeing legacy ones.
    blocked = await h.service.create_access(
        USER, label="", resource_grants={MEMORIES: ["memories:read"]}, client_id=CLIENT,
    )
    assert blocked["error"] == RESIDENT_MIGRATION_CONFLICT
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
async def test_fold_refuses_identity_scope_disagreement_instead_of_creating_two_cards(tmp_path):
    h = _Harness(tmp_path)
    now = int(time.time())
    grantor_card = h.legacy_authority(
        resource=MEMORIES,
        grants=("memories:read",),
        operations=("search",),
        created_at=now - 100,
        expires_at=now + 1000,
    )
    family_card = h.legacy_authority(
        resource=MAIL,
        grants=("mail:read",),
        operations=("read",),
        created_at=now - 50,
        expires_at=now + 1000,
        identity_scope="grantor_identity_family",
    )
    h.persistence.seed(grantor_card, access_token="old-a")
    h.persistence.seed(family_card, access_token="old-b")

    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)

    assert result["ok"] is False
    assert result["error"] == RESIDENT_MIGRATION_CONFLICT
    assert result["reason"] == "identity_scope_conflict"
    assert result["evidence"] == {
        "identity_scopes": ["grantor", "grantor_identity_family"]
    }
    assert await h.card(_profile().access_id) is None
    assert await h.card(grantor_card.access_id) is not None
    assert await h.card(family_card.access_id) is not None


@pytest.mark.asyncio
async def test_fold_refuses_when_policies_cannot_be_seen(tmp_path):
    h = _Harness(tmp_path, policies=False)
    now = int(time.time())
    h.persistence.seed(
        h.legacy_authority(
            resource=MEMORIES, grants=("memories:read",), operations=("search",),
            created_at=now - 100, expires_at=now + 1000,
        ),
        access_token="old-a",
    )
    result = await h.service.migrate_resident_profile(USER, client_id=CLIENT)
    assert result["ok"] is False and result["reason"] == "invocation_policies_unverifiable"
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
async def test_cross_owner_cards_are_invisible_and_immutable(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER, label="lg-react", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search"]}, client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]
    listed = await h.service.list_access(OTHER_USER)
    assert listed["ok"] and listed["items"] == []
    assert (await h.service.update_access(
        OTHER_USER, access_id=access_id, resource_grants={TASKS: ["tasks:use"]}
    ))["error"] == "delegated_access_not_found"
    assert (await h.service.describe_card(OTHER_USER, access_id=access_id))["error"] == "delegated_access_not_found"
    assert (await h.service.revoke_access(OTHER_USER, access_id=access_id)) == {"ok": True, "removed": False}
    assert await h.service.resident_profile_card(grantor_subject=OTHER, client_id=CLIENT) is None
    assert await h.card(access_id) is not None
    # The other grantor's own profile is a different card entirely.
    assert stable_resident_access_id(OTHER, CLIENT) != access_id


@pytest.mark.asyncio
async def test_save_keeps_a_changed_selected_descriptor_suspended_until_accepted(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER, label="lg-react", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search", "delete"]}, client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]
    accepted_before = (await h.card(access_id)).resource_acceptance[TASKS]

    h.catalog.publish(_connections(tasks_delete_description="Delete permanently"))
    listed = await h.service.list_access(USER)
    item = listed["items"][0]
    drift = item["catalog_drift"]
    assert drift["status"] == "changed"
    assert drift["resources"][TASKS]["changed_operations"] == ["delete"]
    assert drift["changed"]["outer_operations"][0]["effect"] == "suspended_until_accepted"

    # A rename does not accept the change.
    renamed = await h.service.update_access(
        USER, access_id=access_id, resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search", "delete"]}, label="planner",
    )
    assert renamed["ok"]
    after_rename = (await h.card(access_id)).resource_acceptance[TASKS]
    assert after_rename.operations["delete"] == accepted_before.operations["delete"]
    assert renamed["access"]["catalog_drift"]["resources"][TASKS]["changed_operations"] == ["delete"]

    # Accepting exactly that operation makes the resource current.
    accepted = await h.service.update_access(
        USER, access_id=access_id, resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search", "delete"]},
        accepted_operations={TASKS: ["delete"]},
    )
    assert accepted["ok"]
    assert accepted["access"]["catalog_drift"]["status"] == "current"
    assert (await h.card(access_id)).resource_acceptance[TASKS].operations["delete"] != accepted_before.operations["delete"]


@pytest.mark.asyncio
async def test_list_reports_profile_identity_and_compatible_offers(tmp_path):
    h = _Harness(tmp_path)
    await h.service.create_access(
        USER, label="lg-react", resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]}, client_id=CLIENT,
    )
    listed = await h.service.list_access(USER)
    item = listed["items"][0]
    assert item["caller_profile"]["agent_id"] == "lg-react"
    assert item["caller_profile"]["access_id"] == item["access_id"]
    assert item["stable_identity"] is True
    offers = {offer["resource"]: offer for offer in item["resource_offers"]}
    assert offers[MEMORIES]["reason"] == "already_on_card"
    assert offers[TASKS]["compatible"] is True
    assert offers[MAIL]["reason"] == "identity_scope_incompatible"
    assert item["resource_acceptance"][MEMORIES]["kind"] == "catalog"

    # A legacy record reads as not yet stable.
    now = int(time.time())
    h.persistence.seed(
        h.legacy_authority(
            resource=TASKS, grants=("tasks:use",), operations=("search",),
            created_at=now - 10, expires_at=now + 1000,
        )
    )
    listed = await h.service.list_access(USER)
    by_id = {entry["access_id"]: entry for entry in listed["items"]}
    assert by_id[agent_grant_access_id(GRANTOR, CLIENT, [TASKS])]["stable_identity"] is False


@pytest.mark.asyncio
async def test_a_spent_one_use_permit_does_not_revive_when_the_operation_is_re_added(tmp_path):
    h = _Harness(tmp_path)
    created = await h.service.create_access(
        USER, label="lg-react", resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search", "delete"]}, client_id=CLIENT,
    )
    access_id = created["access"]["access_id"]
    delete = InvocationAuthority(access_id=access_id, resource=TASKS, surface=SURFACE_OUTER, operation="delete")
    await h.policies.set_policy(owner_subject=GRANTOR, authority=delete, mode=POLICY_ONCE)
    spent = await h.policies.begin(
        owner_subject=GRANTOR, authority=delete, invocation_id="inv-1",
        request_digest=canonical_request_digest({"id": "t-1"}), card_revision=1,
    )
    assert spent.dispatch is True
    removed = await h.service.update_access(
        USER, access_id=access_id, resource_grants={TASKS: ["tasks:use"]}, resource_operations={TASKS: ["search"]},
    )
    assert removed["ok"]
    readded = await h.service.update_access(
        USER, access_id=access_id, resource_grants={TASKS: ["tasks:use"]},
        resource_operations={TASKS: ["search", "delete"]},
    )
    assert readded["ok"]
    decision = await h.policies.begin(
        owner_subject=GRANTOR, authority=delete, invocation_id="inv-2",
        request_digest=canonical_request_digest({"id": "t-2"}), card_revision=3,
    )
    assert decision.allowed is False
    assert decision.reason == "delegated_invocation_limit_exhausted"

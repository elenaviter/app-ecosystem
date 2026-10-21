# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W918 H2: an agent Card's project save is bounded per resource.

The project bounds only what it governs; every other resource is bounded by
the Card owner's own admission-time authority (a verified grantor proof), and
an addition there is refused without one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessRecord, AutomationAccessService
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.grantor_authority_proof import issue_grantor_proof
from connection_hub.delegated_credentials.managed_card_selection_plan import _verified_owner_authority
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
from service_foundation.coordination.durable_decision_log import DecisionRefused

REGISTERED = "kdcube:role:registered"
EDITOR = "docs:editor"
BOARD = "problem_board"
DOCS = "docs"
CONNECTIONS = {"delegated_credentials": {"oauth": {
    "enabled": True,
    "capabilities": [
        {"grant": "work:review", "label": "Review", "delegable_roles": [REGISTERED]},
        {"grant": "work:observe", "label": "Observe", "delegable_roles": [REGISTERED]},
        {"grant": "docs:read", "label": "Read docs", "delegable_roles": [EDITOR]},
    ],
    "resources": [
        {"resource": BOARD, "label": "Problem Board", "tools": {
            "review.approve": {"label": "Approve", "grants": ["work:review"]},
            "work.report": {"label": "Report", "grants": ["work:observe"]}}},
        {"resource": DOCS, "label": "Docs", "tools": {"docs.search": {"label": "Search", "grants": ["docs:read"]}}},
    ],
}}}
OWNER = {"user_id": "user:owner", "roles": [REGISTERED, EDITOR], "permissions": []}
PROJECT_BOUND = ["work:review", "work:observe"]


def _service() -> AutomationAccessService:
    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=CONNECTIONS)

    return AutomationAccessService(redis=object(), tenant="t", project="p",
                                   config=oauth_delegated_config_from_connections(CONNECTIONS),
                                   catalog_resolver=_Resolver())


def _agent(*, docs: bool = False) -> AutomationAccessRecord:
    grants = {BOARD: ("work:review",), **({DOCS: ("docs:read",)} if docs else {})}
    operations = {BOARD: ("review.approve",), **({DOCS: ("docs.search",)} if docs else {})}
    return AutomationAccessRecord(
        access_id="agent-card", label="Agent", client_id="client", grantor_subject="user:owner",
        delegate_subject="agent", card_kind="agent", operations=(), resource_grants=grants,
        resource_operations=operations, named_service_operations=NamedServiceSelection.none(), properties={})


async def _save(existing, *, docs: bool, authority, governed=(BOARD,), bound=PROJECT_BOUND, bounded=True):
    service = _service()
    active = await service._catalog_resolver.resolve_active()
    grants = {BOARD: ["work:review"], **({DOCS: ["docs:read"]} if docs else {})}
    operations = {BOARD: ["review.approve"], **({DOCS: ["docs.search"]} if docs else {})}
    return await service._resolve_card_authority(
        user={"user_id": "user:owner"}, existing=existing, active=active,
        resource_grants=grants, resource_operations=operations, operations=(),
        named_service_operations=None, account_scope=None, properties=None,
        _delegable_grants=bound,
        _grantor_bound={"governed": governed, "authority": authority} if bounded else None)


def _error(resolved):
    return None if resolved.error is None else resolved.error["error"]


@pytest.mark.asyncio
async def test_the_owner_adds_their_own_service_with_a_proven_authority():
    assert _error(await _save(_agent(), docs=True, authority=OWNER)) is None


@pytest.mark.asyncio
async def test_without_a_proof_a_non_project_addition_is_refused_by_name():
    assert _error(await _save(_agent(), docs=True, authority=None)) == "card_grantor_inventory_unavailable"


@pytest.mark.asyncio
async def test_the_owner_cannot_add_what_they_do_not_hold():
    plain = {"user_id": "user:owner", "roles": [REGISTERED], "permissions": []}
    assert _error(await _save(_agent(), docs=True, authority=plain)) == "delegated_access_grants_not_delegable"


@pytest.mark.asyncio
async def test_the_project_still_bounds_what_it_governs():
    # The owner's own authority never widens the project's own service.
    assert _error(await _save(_agent(), docs=False, authority=OWNER, bound=[])) is None  # carried work:review
    service_add = await _save(_agent(), docs=False, authority=OWNER, bound=["work:observe"])
    assert _error(service_add) is None


@pytest.mark.asyncio
async def test_an_admin_save_keeps_or_removes_a_carried_service_without_a_proof():
    assert _error(await _save(_agent(docs=True), docs=True, authority=None)) is None
    assert _error(await _save(_agent(docs=True), docs=False, authority=None)) is None


@pytest.mark.asyncio
async def test_without_a_grantor_bound_the_save_is_bounded_as_before():
    assert _error(await _save(_agent(), docs=True, authority=None, bounded=False)) == (
        "delegated_access_grants_not_delegable")


SECRET = "k" * 40
SELECTION = {"resource_grants": {DOCS: ["docs:read"]}}


def _card():
    return CardAuthority(access_id="agent-card", client_id="client", grantor_subject="user:owner",
                         delegate_subject="agent", source="agent", card_kind="agent", card_revision=4,
                         resource_grants={BOARD: ("work:review",)}, resource_operations={BOARD: ("review.approve",)},
                         named_service_operations=NamedServiceSelection.exact({}), identity_scope="grantor",
                         created_at=1, expires_at=2000000000)


def _token(**over):
    args = dict(secret=SECRET, user=OWNER, grantor_subject="user:owner", access_id="agent-card",
                original_revision=4, request_id="save-1", selection=SELECTION, now=1000, ttl_seconds=300)
    args.update(over)
    return issue_grantor_proof(**args)


def _verify(proof, *, actor="user:owner", selection=SELECTION, now=1010, secret=SECRET):
    return _verified_owner_authority(SimpleNamespace(_grantor_authority_secret=secret), _card(), proof,
                                     selection=selection, actor_subject=actor, now=now)


def test_the_planner_reads_the_owners_identity_from_a_valid_proof():
    identity = _verify({"token": _token(), "business_request_id": "save-1"})
    assert identity["user_id"] == "user:owner" and EDITOR in identity["roles"]
    assert _verify(None) is None


@pytest.mark.parametrize("change", [
    dict(actor="user:admin"), dict(selection={"resource_grants": {}}), dict(now=1300), dict(secret="z" * 40),
])
def test_the_planner_refuses_a_proof_for_another_actor_edit_time_or_key(change):
    with pytest.raises(DecisionRefused, match="card_grantor_proof_invalid"):
        _verify({"token": _token(), "business_request_id": "save-1"}, **change)


@pytest.mark.parametrize("proof", [
    {"token": "x.y", "business_request_id": "save-1"},
    {"token": None, "business_request_id": "save-1"},
    {"token": "x"},
    "not-a-mapping",
])
def test_the_planner_refuses_a_malformed_or_tampered_proof(proof):
    with pytest.raises(DecisionRefused):
        _verify(proof)


def test_no_secret_means_no_proof_verifies():
    with pytest.raises(DecisionRefused, match="card_grantor_inventory_unavailable"):
        _verify({"token": _token(), "business_request_id": "save-1"}, secret="")


@pytest.mark.asyncio
async def test_the_project_governs_exactly_what_its_control_lists():
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding
    from connection_hub.delegated_credentials.managed_card_selection_plan import _governed_resources

    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    seen = {}

    async def load(subject_hash, access_id):
        seen["at"] = (access_id, subject_hash)
        return SimpleNamespace(resource_grants={BOARD: ("work:review",), "board_admin": ("work:admin",)})

    card = _card().__class__(**{**_card().__dict__, "control_card": ControlCardBinding(
        control_id="p-control", issuer_ref="work:project:x", issuer_kind="application", control_revision=2,
        holder_subject="")})
    assert await _governed_resources(load, card) == ("board_admin", BOARD)
    # Empty holder: P's holder is the Card's own grantor.
    assert seen["at"] == ("p-control", subject_hash_for("user:owner"))

    async def missing(subject_hash, access_id):
        return None

    for loader in (missing, None):
        with pytest.raises(DecisionRefused, match="card_plan_project_control_read_missing"):
            await _governed_resources(loader, card)


def test_the_hub_issues_a_proof_only_for_the_owners_own_agent_card_save():
    from connection_hub.delegated_credentials.grantor_authority_proof import verify_grantor_proof

    service = _service()
    record = _agent()
    args = dict(record=record, kind="agent_card", revision=4, request_id="save-1", selection=SELECTION)
    assert service._owner_authority_proof(OWNER, **args) is None  # no secret configured: dormant
    service._grantor_authority_secret = SECRET
    assert service._owner_authority_proof({**OWNER, "user_id": "user:admin"}, **args) is None
    assert service._owner_authority_proof(OWNER, **{**args, "kind": "project_control"}) is None
    token = service._owner_authority_proof(OWNER, **args)
    import time

    authority = verify_grantor_proof(token, secret=SECRET, now=int(time.time()), grantor_subject="user:owner",
                                     access_id="agent-card", original_revision=4, request_id="save-1",
                                     selection=SELECTION)
    assert EDITOR in authority.roles


def test_stage_fences_the_project_control_an_agent_cards_bound_used():
    from connection_hub.delegated_credentials.cards.participant_card_version import (
        CardVersionRefused, _agent_parent_reads,
    )
    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    p_key = (subject_hash_for("user:owner"), "p-control")
    updates = [{"kind": "reselect_agent_card", "subject_hash": "a" * 64, "access_id": "agent-card"}]
    cards = [{"subject_hash": "a" * 64, "access_id": "agent-card", "candidate": {
        "grantor_subject": "user:owner", "control_card": {"control_id": "p-control", "holder_subject": ""}}}]
    reads = [{"subject_hash": p_key[0], "access_id": "p-control", "revision": 7},
             {"subject_hash": "x" * 64, "access_id": "other", "revision": 1}]
    assert _agent_parent_reads(updates, cards, reads) == [
        {"card": {"subject_hash": p_key[0], "access_id": "p-control"}, "version": 7}]
    # A plan that never read P cannot stage unfenced; other kinds add nothing.
    with pytest.raises(CardVersionRefused):
        _agent_parent_reads(updates, cards, reads[1:])
    assert _agent_parent_reads([{**updates[0], "kind": "reselect"}], cards, reads) == []

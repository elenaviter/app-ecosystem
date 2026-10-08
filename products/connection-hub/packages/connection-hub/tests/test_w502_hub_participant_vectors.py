"""W502 (EMain C1): the shared connection-hub.card vectors, pinned on the Hub side.

``fixtures/w502_hub_participant_vectors.json`` is the one input shape the Hub
stages; Problem Board pins the same file for the projection it writes. The
accepted vector must stage through the verified authority path, and every
refused variant (one field changed) must never stage.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, DecisionRefused, IntentDraft
from service_foundation.coordination.durable_wire import participant_projection

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.authority_intent_source import (
    AuthorityCardIntentSource,
    AuthorityDecisionReader,
    CardAuthorityBinding,
)
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, candidate_value, candidate_value_digest, hub_participant_input,
)
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
    PROTOCOL, TransactionAuthorityRefused, card_authority_signature,
)
from test_card_transaction_store import _Applier, _setup

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "w502_hub_participant_vectors.json").read_text())
TX = "b" * 64
SECRET = "v" * 40
AUTHORITY = CardAuthorityBinding(secret=SECRET, service_id="problem-board@1-0", audience="connection-hub@1-0")
NOW = 1_800_000_000


def test_the_hub_input_builder_reproduces_the_shared_vector_exactly() -> None:
    accepted = VECTORS["accepted"]
    value = accepted["candidate_value"]
    original = CardAuthority.from_mapping({**value["candidate"], "card_revision": value["original_revision"],
                                           "label": "CI bot"})
    candidate = CardAuthority.from_mapping(value["candidate"])
    rebuilt = hub_participant_input(
        original=original, candidate=candidate, subject_hash=subject_hash_for(VECTORS["grantor_subject"]),
        action="update", actor_subject="user:project-admin", actor_kind="caller", effects=value["effects"])
    assert candidate_value(original=original, candidate=candidate, effects=value["effects"]) == value
    assert candidate_value_digest(value) == accepted["candidate_digest"]
    assert rebuilt == accepted["participant_input"]


def _response(record, phase, request_echo):
    candidates = record.intent.as_mapping()["payload"]["participant_candidates"]
    unsigned = {
        "schema": PROTOCOL, "phase": phase, "request_echo": request_echo, "audience": AUTHORITY.audience,
        "participant": PARTICIPANT, "global_intent_bytes": record.intent.canonical_bytes.decode("utf-8"),
        "global_intent_digest": record.intent.digest, "projection": participant_projection(record.intent, PARTICIPANT),
        "candidate": candidates[PARTICIPANT], "decision": "undecided", "decided_at": None,
    }
    return {**unsigned, "authority_proof": {
        "service_id": AUTHORITY.service_id, "timestamp": str(NOW),
        "signature": card_authority_signature(unsigned, secret=SECRET, service_id=AUTHORITY.service_id,
                                              timestamp=str(NOW))}}


async def _stage(tmp_path, participant_input, *, before_stage=None):
    store, service, before, after = await _setup(tmp_path)
    service.bind_effect_applier(_Applier())
    if before_stage is not None:
        await before_stage(service, before)
    accepted = VECTORS["accepted"]
    draft = IntentDraft(replay_scope="pb:vectors", request_id="pb-vector", expires_at=NOW + 600,
                        participants=(PARTICIPANT,),
                        payload={"participant_inputs": {PARTICIPANT: participant_input},
                                 "participant_candidates": {PARTICIPANT: accepted["candidate_value"]}})
    record = DecisionRecord(draft.bind(TX, 1), "preparing", {}, {})

    async def fetch(transaction_id, phase, request_echo):
        if phase == "decision":  # PB's endpoint: nothing decided yet
            raise TransactionAuthorityRefused("authority_decision_pending")
        return _response(record, phase, request_echo)

    clock = lambda: NOW  # noqa: E731
    decisions = AuthorityDecisionReader(fetch=fetch, authority=AUTHORITY, clock=clock)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub = HubCardParticipant(service=service, store=store,
                             intents=AuthorityCardIntentSource(store=store, fetch=fetch, authority=AUTHORITY,
                                                               clock=clock), decisions=decisions)
    return hub, store, before


@pytest.mark.asyncio
async def test_the_accepted_vector_stages(tmp_path):
    hub, store, before = await _stage(tmp_path, VECTORS["accepted"]["participant_input"])
    receipt = await hub.prepare(TX)
    assert receipt.candidate_digest == VECTORS["accepted"]["candidate_digest"]
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", VECTORS["refused"], ids=[v["name"] for v in VECTORS["refused"]])
async def test_each_refused_variant_never_stages(tmp_path, variant):
    # EMain N1 included: actor_kind "human" on the authority path, where the
    # recorded actor_kind is copied from the projection, is refused by the allow-list.
    changed = copy.deepcopy(VECTORS["accepted"]["participant_input"])
    changed[variant["field"]] = variant["value"]
    try:
        hub, store, before = await _stage(tmp_path, changed)
    except Exception as exc:  # the kernel's own projection rules may refuse when the draft is built
        assert "invalid" in str(exc) or "revision" in str(exc)
        return
    with pytest.raises(DecisionRefused):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_a_pb_initiated_intent_carries_its_reads_through_the_authority_source(tmp_path):
    # EMain #603: the reads come from the VERIFIED projection, so a PB intent naming a
    # dependency Card (present at r1) and an absent My Card stages and holds both.
    scope = VECTORS["accepted"]["participant_input"]["target_scope"]
    changed = copy.deepcopy(VECTORS["accepted"]["participant_input"])
    changed["dependency_revisions"] = {f"card:{scope}:aut_dependency": 1, f"card-absent:{scope}:aut_missing": 1}
    held = {}

    async def dependency(service, before):
        held["card"] = replace(before, access_id="aut_dependency", card_revision=1, label="person Control")
        await service.commit(held["card"], subject_hash=scope, expected_revision=0, now=NOW)
        held["service"] = service

    hub, store, before = await _stage(tmp_path, changed, before_stage=dependency)
    await hub.prepare(TX)
    assert [(read["access_id"], read["revision"]) for read in (await tx.state(store, transaction_id=TX))["reads"]]         == [("aut_dependency", 1), ("aut_missing", 0)]
    moved = replace(held["card"], card_revision=2, label="another admin's edit")
    with pytest.raises(Exception, match="unresolved"):
        await held["service"].commit(moved, subject_hash=scope, expected_revision=1, now=NOW)


# ── the accepted vector with read reservations and the catalog key (CodeApp's shared input) ──

RESERVED = VECTORS["accepted_with_reservations"]


def test_the_hub_builder_reproduces_the_vector_with_reservations():
    from connection_hub.delegated_credentials.cards.card_participant import catalog_reservation_from_dependencies
    from connection_hub.delegated_credentials.cards.card_participant import reads_from_dependencies
    from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest

    value = RESERVED["candidate_value"]
    original = CardAuthority.from_mapping({**value["candidate"], "card_revision": value["original_revision"],
                                           "label": "CI bot"})
    rebuilt = hub_participant_input(
        original=original, candidate=CardAuthority.from_mapping(value["candidate"]),
        subject_hash=subject_hash_for(VECTORS["grantor_subject"]), action="update",
        actor_subject="user:project-admin", actor_kind="caller", effects=value["effects"],
        reads=RESERVED["reads"], catalog_version_digest=RESERVED["catalog"]["digest"])
    assert rebuilt == RESERVED["participant_input"]
    assert catalog_version_digest(RESERVED["catalog"]["version"],
                                  RESERVED["catalog"]["content_hash"]) == RESERVED["catalog"]["digest"]
    dependencies = RESERVED["participant_input"]["dependency_revisions"]
    assert reads_from_dependencies(dependencies) == sorted(RESERVED["reads"],
                                                           key=lambda r: (r["subject_hash"], r["access_id"]))
    assert catalog_reservation_from_dependencies(dependencies) == RESERVED["catalog"]["digest"]
    assert RESERVED["candidate_digest"] == VECTORS["accepted"]["candidate_digest"]


@pytest.mark.asyncio
async def test_the_vector_with_reservations_stages_through_the_authority_path(tmp_path):
    from types import SimpleNamespace

    from connection_hub.delegated_credentials.catalog.reservations import CatalogReservations

    scope = RESERVED["participant_input"]["target_scope"]
    control = {}

    async def dependencies(service, before):
        control["card"] = replace(before, access_id=RESERVED["reads"][0]["access_id"], card_revision=1)
        for revision in (1, 2, 3):
            card = replace(control["card"], card_revision=revision, label=f"person Control r{revision}")
            await service.commit(card, subject_hash=scope, expected_revision=revision - 1, now=NOW)

    hub, store, before = await _stage(tmp_path, RESERVED["participant_input"], before_stage=dependencies)

    async def read_active():
        return SimpleNamespace(version=RESERVED["catalog"]["version"],
                               content_hash=RESERVED["catalog"]["content_hash"])

    reservations = CatalogReservations(SimpleNamespace(root=tmp_path / "catalog", read_active=read_active))
    tx.bind_catalog_reservations(store, reservations)
    receipt = await hub.prepare(TX)
    assert receipt.candidate_digest == RESERVED["candidate_digest"]
    staged = await tx.state(store, transaction_id=TX)
    assert staged["catalog"] == RESERVED["catalog"]["digest"]
    assert sorted((r["access_id"], r["revision"]) for r in staged["reads"]) == sorted(
        (r["access_id"], r["revision"]) for r in RESERVED["reads"])
    assert [fence["transaction_id"] for fence in await reservations.holders()] == [TX]

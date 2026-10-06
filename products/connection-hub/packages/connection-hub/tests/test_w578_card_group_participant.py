"""W578: a card group through the Hub participant, from a Problem Board-initiated transaction.

The authority (a stand-in for Problem Board's signer) answers with the group
participant input and candidate (the #627 shape) in its persisted intent; the
Hub verifies the signed response, builds the group intent from ITS OWN Cards
(a present member at exactly its base revision, an absent member still
absent), prepares every member under the one decision, and finishes or
recovers the group by its own id only.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, DecisionRefused, IntentDraft
from service_foundation.coordination.durable_wire import participant_projection

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.authority_intent_source import (
    AuthorityCardIntentSource, AuthorityDecisionReader, CardAuthorityBinding,
)
from connection_hub.delegated_credentials.cards.card_group import group_candidate_value, hub_group_participant_input
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
    PROTOCOL, TransactionAuthorityRefused, card_authority_signature,
)
from connection_hub.delegated_credentials.catalog.reservations import CatalogReservations, catalog_version_digest
from test_card_transaction_store import _setup
from test_w578_hub_card_group_vectors import CREATOR, P_ID, P_READ, PROJECT, _card, _members

TX = "f" * 64
SECRET = "v" * 40
AUTHORITY = CardAuthorityBinding(secret=SECRET, service_id="problem-board@1-0", audience="connection-hub@1-0")
NOW = 1_800_000_000
CATALOG_VERSION, CATALOG_HASH = "catalog-v1", "h" * 64


def _group(members=None, reads=(P_READ,)):
    members = members if members is not None else _members()
    catalog = catalog_version_digest(CATALOG_VERSION, CATALOG_HASH)
    return (hub_group_participant_input(members=members, actor_subject="platform-user-2", actor_kind="grantor",
                                        reads=reads, catalog_version_digest=catalog),
            group_candidate_value(members))


class _Authority:
    """Problem Board's authority for one transaction: undecided until ``decide``."""

    def __init__(self, participant_input, candidate_value):
        draft = IntentDraft(replay_scope="pb:group", request_id="pb-group", expires_at=NOW + 600,
                            participants=(PARTICIPANT,),
                            payload={"participant_inputs": {PARTICIPANT: participant_input},
                                     "participant_candidates": {PARTICIPANT: candidate_value}})
        self.record = DecisionRecord(draft.bind(TX, 1), "preparing", {}, {})
        self.decision = None

    async def fetch(self, transaction_id, phase, request_echo):
        if transaction_id != TX:
            raise TransactionAuthorityRefused("authority_transaction_unknown")
        if phase == "decision" and self.decision is None:
            raise TransactionAuthorityRefused("authority_decision_pending")
        candidates = self.record.intent.as_mapping()["payload"]["participant_candidates"]
        unsigned = {
            "schema": PROTOCOL, "phase": phase, "request_echo": request_echo, "audience": AUTHORITY.audience,
            "participant": PARTICIPANT, "global_intent_bytes": self.record.intent.canonical_bytes.decode("utf-8"),
            "global_intent_digest": self.record.intent.digest,
            "projection": participant_projection(self.record.intent, PARTICIPANT),
            "candidate": candidates[PARTICIPANT],
            "decision": self.decision if phase == "decision" else "undecided",
            "decided_at": NOW if phase == "decision" else None,
        }
        return {**unsigned, "authority_proof": {
            "service_id": AUTHORITY.service_id, "timestamp": str(NOW),
            "signature": card_authority_signature(unsigned, secret=SECRET, service_id=AUTHORITY.service_id,
                                                  timestamp=str(NOW))}}


async def _hub(tmp_path, participant_input=None, candidate_value=None, *, pending=True, plain=False):
    store, service, before, after = await _setup(tmp_path)
    # The project's P (held by its creator) and, for a redemption, the pending invitation Card at r1.
    project_control = _card(access_id=P_ID, grantor_subject=CREATOR, client_id="control-card:project",
                            delegate_subject="", source="control", card_kind="control", card_revision=1,
                            issuer_ref=PROJECT, issuer_kind="application")
    await service.commit(project_control, subject_hash=subject_hash_for(CREATOR), expected_revision=0, now=NOW)
    if pending:
        claim = next(member for member in _members() if not member["original_absent"])
        original = _card(**{**claim["candidate"], "card_revision": 1, "state": "active"})
        await service.commit(original, subject_hash=claim["subject_hash"], expected_revision=0, now=NOW)
    plain_members = None
    if plain:
        # A plain group: the harness Card's update and a newly minted Card (no Control chains);
        # the project chain composes with real builders in the planner tests.
        from connection_hub.delegated_credentials.cards.card_group import group_member
        created = replace(after, access_id="aut_zz_new", card_revision=1, label="created in the group")
        plain_members = [group_member(original=before, candidate=after, action="update"),
                         group_member(original=None, candidate=created, action="create")]
        participant_input, candidate_value = _group(plain_members)
    elif participant_input is None:
        participant_input, candidate_value = _group()
    authority = _Authority(participant_input, candidate_value)
    clock = lambda: NOW  # noqa: E731
    decisions = AuthorityDecisionReader(fetch=authority.fetch, authority=AUTHORITY, clock=clock)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))

    async def read_active():
        return SimpleNamespace(version=CATALOG_VERSION, content_hash=CATALOG_HASH)

    tx.bind_catalog_reservations(store, CatalogReservations(SimpleNamespace(root=tmp_path / "catalog",
                                                                            read_active=read_active)))
    hub = HubCardParticipant(service=service, store=store,
                             intents=AuthorityCardIntentSource(store=store, fetch=authority.fetch,
                                                               authority=AUTHORITY, clock=clock),
                             decisions=decisions)
    hub.plain_members = plain_members
    return hub, store, authority


async def _read(store, member):
    found = await store.read_current_authority(subject_hash=member["subject_hash"], access_id=member["access_id"])
    return None if found is None else found[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_pb_initiated_group_prepares_and_finishes_under_its_one_decision(tmp_path, decision):
    hub, store, authority = await _hub(tmp_path, plain=True)
    receipt = await hub.prepare(TX)
    assert receipt.candidate_digest == authority.record.intent.as_mapping()["payload"][
        "participant_inputs"][PARTICIPANT]["candidate_digest"]
    group = await tx.state(store, transaction_id=TX)
    assert group["staged"] is True and len(group["members"]) == 2
    assert [r.transaction_id for r in await hub.list_prepared(limit=10)] == [TX]
    assert (await hub.read_pending(TX)).transaction_id == TX
    authority.decision = decision
    await hub.finish(TX, decision)
    assert (await tx.state(store, transaction_id=TX))["state"] == decision and await tx.list_in_doubt(store) == []
    for member in hub.plain_members:
        current = await _read(store, member)
        if decision == "committed":
            assert current.to_dict() == member["candidate"]
        elif member["original_absent"]:
            assert current is None
        else:
            assert current.card_revision == member["original_revision"]


@pytest.mark.asyncio
async def test_a_member_moved_since_the_plan_never_stages(tmp_path):
    hub, store, _authority = await _hub(tmp_path, pending=False)  # the claimed pending Card is missing
    with pytest.raises(DecisionRefused, match="card_intent_base_moved"):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_an_absent_member_created_meanwhile_never_stages(tmp_path):
    members = _members()
    hub, store, _authority = await _hub(tmp_path)
    created = next(member for member in members if member["original_absent"])
    from connection_hub.delegated_credentials.cards.model import CardAuthority
    await hub._service.commit(CardAuthority.from_mapping(created["candidate"]), subject_hash=created["subject_hash"],
                              expected_revision=0, now=NOW)
    with pytest.raises(DecisionRefused, match="card_intent_base_moved"):
        await hub.prepare(TX)


@pytest.mark.asyncio
async def test_a_tampered_group_candidate_is_refused_by_the_verifier(tmp_path):
    participant_input, value = _group()
    tampered = copy.deepcopy(value)
    tampered["cards"][0]["candidate"]["label"] = "widened elsewhere"
    hub, store, _authority = await _hub(tmp_path, participant_input, tampered)
    with pytest.raises(DecisionRefused, match="authority_candidate_invalid"):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_a_group_without_its_p_read_is_refused_before_any_write(tmp_path):
    members = _members()
    value = group_candidate_value(members)
    participant_input, _ = _group()
    participant_input = {**participant_input, "dependency_revisions": {
        key: revision for key, revision in participant_input["dependency_revisions"].items()
        if not key.startswith("card:")}}
    hub, store, _authority = await _hub(tmp_path, participant_input, value)
    with pytest.raises(DecisionRefused):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_an_abort_before_any_stage_tombstones_the_group_and_a_late_prepare_refuses(tmp_path):
    hub, store, authority = await _hub(tmp_path)
    await hub._intents.load(TX)  # the intent was recorded, then the coordinator aborted before prepare
    authority.decision = "aborted"
    receipt = await hub.finish(TX, "aborted")
    assert receipt.transaction_id == TX and await tx.state(store, transaction_id=TX) is None
    with pytest.raises(DecisionRefused):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_an_unstaged_group_is_never_reported_as_prepared(tmp_path):
    hub, store, _authority = await _hub(tmp_path)
    intent = await hub._intents.load(TX)
    await tx.begin_group(store, transaction_id=TX, intent_digest=intent.intent_digest, participant=PARTICIPANT,
                         members=[(member.subject_hash, member.candidate.access_id) for member in intent.members])
    assert await hub.read_pending(TX) is None and await hub.list_prepared(limit=10) == []
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [TX]


@pytest.mark.asyncio
async def test_a_group_whose_chain_does_not_compose_never_writes(tmp_path):
    """The vector's members are contract stand-ins: their Control chain does not compose, so nothing stages."""
    hub, store, _authority = await _hub(tmp_path)
    with pytest.raises(DecisionRefused, match="card_group_chain_invalid"):
        await hub.prepare(TX)
    group = await tx.state(store, transaction_id=TX)
    assert group is None

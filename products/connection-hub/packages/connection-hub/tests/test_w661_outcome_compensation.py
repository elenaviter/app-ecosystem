"""W661 contract v6.3 items 5 and 7 (Ops, piece 1): the read-only outcome and the fenced compensation."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from test_card_service import _Cache
from test_project_person_control import _authority as _person
from test_w661_card_versions import DIGEST, TXN, WHEN, _links, _setup

BINDING = {"scope": "project-a", "caller": "pb"}


async def _person_world(tmp_path):
    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    member = _person(revision=1, operations=("review.accept", "project.people.set_role"))
    subject_hash = hashlib.sha256(member.grantor_subject.encode()).hexdigest()
    await service.commit(member, subject_hash=subject_hash, expected_revision=0, now=1_780_000_000)
    demoted = replace(member, card_revision=2, resource_operations={k: ("review.accept",)
                                                                     for k in member.resource_operations})
    return store, service, subject_hash, member, demoted


async def _demote(service, subject_hash, member, demoted, txn=TXN):
    answer = await service.stage_card_version(txn=txn, request_digest=DIGEST, catalog="c", now=WHEN,
                                              members=[(subject_hash, member.access_id, 1, demoted)], binding=BINDING)
    await service.publish_card_version(txn=txn, binding=BINDING)
    return _links(answer)


async def _current(store, subject_hash, card):
    found = await store.read_current_authority(subject_hash=subject_hash, access_id=card.access_id)
    return None if found is None else found[1]


def _tree(store):
    return sorted(str(p.relative_to(store.root)) for p in store.root.rglob("*"))


# ---- item 5: outcome --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_outcome_reports_each_stage_of_a_save_and_writes_nothing(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    from test_w661_card_versions import SUBJECT_HASH

    tree = _tree(store)
    assert await service.outcome_card_version(txn=TXN) == {"state": "unknown_txn", "members": []}
    assert _tree(store) == tree
    answer = await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                              members=[(SUBJECT_HASH, before.access_id, 1, after)], binding=BINDING)
    staged = await service.outcome_card_version(txn=TXN, binding=BINDING, links=_links(answer), at=WHEN)
    assert staged["state"] == "staged" and [m["current"] for m in staged["members"]] == [False]
    tree = _tree(store)
    await service.outcome_card_version(txn=TXN, binding=BINDING, links=_links(answer), at=WHEN)
    assert _tree(store) == tree  # read-only
    await service.publish_card_version(txn=TXN, binding=BINDING)
    published = await service.outcome_card_version(txn=TXN, binding=BINDING, links=_links(answer), at=WHEN)
    assert published == {"state": "published", "members": [{**_links(answer)[0], "current": True}]}
    await service.commit(replace(after, card_revision=3, label="successor"), subject_hash=SUBJECT_HASH,
                         expected_revision=2, now=1_780_000_200)
    superseded = await service.outcome_card_version(txn=TXN, binding=BINDING, links=_links(answer), at=WHEN)
    assert superseded["state"] == "published" and superseded["members"][0]["current"] is False
    with pytest.raises(tx.CardTransactionRefused, match="txn_scope_mismatch"):
        await service.outcome_card_version(txn=TXN, binding={"scope": "project-b", "caller": "pb"},
                                           links=_links(answer), at=WHEN)


# ---- item 7: compensate -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_compensation_restores_the_previous_content_as_a_new_version_and_a_retry_is_idempotent(tmp_path):
    store, service, subject_hash, member, demoted = await _person_world(tmp_path)
    links = await _demote(service, subject_hash, member, demoted)
    assert (await _current(store, subject_hash, member)).resource_operations == demoted.resource_operations
    at2 = WHEN + timedelta(seconds=5)
    answer = await service.compensate_card_version(txn=TXN, binding=BINDING, links=links, at=WHEN,
                                                   compensation_at=at2)
    assert answer["state"] == "compensated" and answer["members"][0]["version"] == 3
    restored = await _current(store, subject_hash, member)
    assert restored == replace(member, card_revision=3)  # the earlier content, as a NEW version: never a rewind
    history = await store.list_revision_names(subject_hash=subject_hash, access_id=member.access_id)
    assert len(history) == 3  # 1 (member), 2 (demoted), 3 (restored): nothing was deleted
    again = await service.compensate_card_version(txn=TXN, binding=BINDING, links=links, at=WHEN,
                                                  compensation_at=at2)
    assert again == {"state": "already_compensated", "members": answer["members"]}
    assert len(await store.list_revision_names(subject_hash=subject_hash, access_id=member.access_id)) == 3


@pytest.mark.asyncio
async def test_a_compensation_never_overwrites_a_successor(tmp_path):
    store, service, subject_hash, member, demoted = await _person_world(tmp_path)
    links = await _demote(service, subject_hash, member, demoted)
    successor = replace(demoted, card_revision=3, label="a later save")
    await service.commit(successor, subject_hash=subject_hash, expected_revision=2, now=1_780_000_300)
    tree = _tree(store)
    with pytest.raises(tx.CardTransactionRefused, match="compensation_superseded"):
        await service.compensate_card_version(txn=TXN, binding=BINDING, links=links, at=WHEN,
                                              compensation_at=WHEN + timedelta(seconds=5))
    assert _tree(store) == tree and await _current(store, subject_hash, member) == successor


@pytest.mark.asyncio
async def test_a_credential_bearing_card_is_never_compensated_by_content(tmp_path):
    store, service, before, after = await _setup(tmp_path)  # an automation Card: credential-bearing
    from test_w661_card_versions import SUBJECT_HASH

    answer = await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                              members=[(SUBJECT_HASH, before.access_id, 1, after)], binding=BINDING)
    await service.publish_card_version(txn=TXN, binding=BINDING)
    tree = _tree(store)
    with pytest.raises(tx.CardTransactionRefused, match="compensation_unsupported"):
        await service.compensate_card_version(txn=TXN, binding=BINDING, links=_links(answer), at=WHEN,
                                              compensation_at=WHEN + timedelta(seconds=5))
    assert _tree(store) == tree


@pytest.mark.asyncio
async def test_a_compensation_needs_the_same_binding_and_the_exact_link(tmp_path):
    store, service, subject_hash, member, demoted = await _person_world(tmp_path)
    links = await _demote(service, subject_hash, member, demoted)
    with pytest.raises(tx.CardTransactionRefused, match="txn_scope_mismatch"):
        await service.compensate_card_version(txn=TXN, binding={"scope": "project-b", "caller": "pb"}, links=links,
                                              at=WHEN, compensation_at=WHEN + timedelta(seconds=5))
    wrong = [{**links[0], "checksum": links[0]["checksum"][:-1] + ("0" if links[0]["checksum"][-1] != "0" else "1")}]
    with pytest.raises(tx.CardTransactionRefused, match="compensation_superseded|card_version_link_mismatch"):
        await service.compensate_card_version(txn=TXN, binding=BINDING, links=wrong, at=WHEN,
                                              compensation_at=WHEN + timedelta(seconds=5))
    assert (await _current(store, subject_hash, member)) == demoted


@pytest.mark.asyncio
async def test_the_port_maps_outcome_and_compensate_to_links(tmp_path):
    from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore

    class _Refused(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.code = code

    store, service, subject_hash, member, demoted = await _person_world(tmp_path)
    links = await _demote(service, subject_hash, member, demoted)
    port = ServiceCardVersionStore(service, refused=_Refused)
    wire = [{"card": {"subject_hash": l["subject_hash"], "access_id": l["access_id"]}, "version": l["version"],
             "checksum": l["checksum"]} for l in links]
    outcome = await port.outcome(TXN, scope="project-a", caller="pb", links=wire, at=WHEN)
    assert outcome == {"state": "published", "members": [{**wire[0], "current": True}]}
    done = await port.compensate(TXN, scope="project-a", caller="pb", links=wire, at=WHEN,
                                 compensation_at=WHEN + timedelta(seconds=5))
    assert done["state"] == "compensated" and done["members"][0]["version"] == 3
    with pytest.raises(_Refused) as refused:
        await port.compensate(TXN, scope="project-b", caller="pb", links=wire, at=WHEN,
                              compensation_at=WHEN + timedelta(seconds=5))
    assert refused.value.code == "txn_scope_mismatch"

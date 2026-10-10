"""W661 pieces 1 + 2: the card_version handler on the real Card version store.

CardVersionOperation (piece 2) drives ServiceCardVersionStore over DelegatedCardService and
BundleStorageDelegatedCardStore (piece 1). Only the planner (its output is a real Card) and the
catalog store are stand-ins. Covers the save, ROLLBACK leaving nothing, a lost PUBLISH reply, the
scope binding on the real marker, a fresh-echo retry, the catalog under the lock and a stale base.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore
from connection_hub.delegated_credentials.cards.participant_card_version import CardVersionRefused
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from test_card_transaction_store import SUBJECT_HASH, _setup
import test_w661_card_version_operation as p2

CATALOG = p2.CATALOG


class _Catalogs:
    def __init__(self, catalog=CATALOG):
        self.catalog = catalog

    async def read_active(self):
        return SimpleNamespace(version=self.catalog["version"], content_hash=self.catalog["content_hash"])


def _plan(before, after):
    return {"ok": True, "plan": {
        "catalog_digest": catalog_version_digest(CATALOG["version"], CATALOG["content_hash"]),
        "candidate_value": {"cards": [{"subject_hash": SUBJECT_HASH, "access_id": after.access_id,
                                       "action": "reselect", "original_revision": before.card_revision,
                                       "original_absent": False, "candidate": after.to_dict()}]}}}


async def _world(tmp_path, *, catalogs=None):
    store, service, before, after = await _setup(tmp_path)
    card_versions = ServiceCardVersionStore(service, refused=CardVersionRefused, catalog_store=catalogs or _Catalogs())
    operation, _, _ = p2._operation(planner=p2._Planner(_plan(before, after)), store=card_versions)
    return store, operation, before, after


def _update(before):
    return [{**p2.UPDATE, "access_id": before.access_id, "subject_hash": SUBJECT_HASH,
             "original_revision": before.card_revision}]


async def _call(operation, op, **override):
    request = p2._request(op, **override)
    return p2._verified(await operation.answer(request), request)


async def _current(store, card):
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    return None if found is None else found[1]


def _files(store, card):
    folder = pathlib.Path(store.card_path(subject_hash=SUBJECT_HASH, access_id=card.access_id)) / "revisions"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.mark.asyncio
async def test_a_save_stages_publishes_and_leaves_only_its_version(tmp_path):
    store, operation, before, after = await _world(tmp_path)
    files_before = _files(store, before)
    staged = await _call(operation, "stage", updates=_update(before))
    assert staged["kind"] == "staged" and staged["members"] == [{
        "card": {"subject_hash": SUBJECT_HASH, "access_id": before.access_id}, "version": after.card_revision,
        "checksum": after.content_hash()}]
    assert await _current(store, before) == before  # D2: not yet final, the old version is active
    published = await _call(operation, "publish")
    assert published == {"kind": "published", "members": staged["members"]}
    assert await _current(store, before) == after
    assert not tx.card_version_marker_path(store, p2.TXN).exists()  # D2: no marker after PUBLISH
    assert len(_files(store, before)) == len(files_before) + 1


@pytest.mark.asyncio
async def test_a_failed_save_rolls_back_to_nothing(tmp_path):
    store, operation, before, _ = await _world(tmp_path)
    files_before = _files(store, before)
    staged = await _call(operation, "stage", updates=_update(before))
    assert await _call(operation, "rollback", links=staged["members"]) == {"kind": "rollback", "state": "rolled_back"}
    assert _files(store, before) == files_before and await _current(store, before) == before
    assert not tx.card_version_marker_path(store, p2.TXN).exists()
    assert await _call(operation, "publish") == {"kind": "refused", "code": "txn_unknown", "status": 404}


@pytest.mark.asyncio
async def test_a_lost_publish_reply_rolls_back_as_already_published(tmp_path):
    store, operation, before, after = await _world(tmp_path)
    staged = await _call(operation, "stage", updates=_update(before))
    await _call(operation, "publish")
    assert await _call(operation, "rollback", links=staged["members"]) == {
        "kind": "rollback", "state": "already_published"}
    assert await _current(store, before) == after
    # Without STAGE's links (PB never got STAGE's answer), the store has nothing to find.
    assert await _call(operation, "rollback", links=[]) == {"kind": "rollback", "state": "unknown_txn"}


@pytest.mark.asyncio
async def test_another_scope_can_neither_publish_nor_clean_up_the_txn(tmp_path):
    store, operation, before, _ = await _world(tmp_path)
    staged = await _call(operation, "stage", updates=_update(before))
    for op, override in (("publish", {}), ("rollback", {"links": staged["members"]})):
        assert await _call(operation, op, scope="work:project:other", **override) == {
            "kind": "refused", "code": "request_scope_invalid", "status": 403}
    assert tx.card_version_marker_path(store, p2.TXN).exists() and await _current(store, before) == before


@pytest.mark.asyncio
async def test_a_fresh_echo_retry_is_the_same_stage_and_a_changed_edit_conflicts(tmp_path):
    store, operation, before, _ = await _world(tmp_path)
    first = await _call(operation, "stage", updates=_update(before))
    files = _files(store, before)
    assert await _call(operation, "stage", updates=_update(before)) == first
    assert _files(store, before) == files
    changed = [{**_update(before)[0], "selection": {"resource_grants": ["other:read"]}}]
    assert (await _call(operation, "stage", updates=changed))["code"] == "stage_txn_conflict"


@pytest.mark.asyncio
async def test_the_catalog_is_checked_under_the_card_lock(tmp_path):
    store, operation, before, _ = await _world(tmp_path, catalogs=_Catalogs({**CATALOG, "version": "catalog-8"}))
    assert (await _call(operation, "stage", updates=_update(before)))["code"] == "stage_catalog_moved"
    assert not tx.card_version_marker_path(store, p2.TXN).exists()



# Two saves of one Card that stage the same next version with the same content at the same `at`
# (millisecond stamp) name the SAME version file today, because the name is f(card, version,
# checksum, at) without the txn. Each test below states the required behaviour.
OTHER = "txn-" + "b" * 40


@pytest.mark.asyncio
async def test_a_stale_publish_is_refused_card_changed(tmp_path):
    store, operation, before, after = await _world(tmp_path)
    await _call(operation, "stage", updates=_update(before))
    await _call(operation, "stage", txn=OTHER, updates=_update(before))
    await _call(operation, "publish", txn=OTHER)
    assert (await _call(operation, "publish"))["code"] == "card_changed"


@pytest.mark.asyncio
async def test_rolling_back_a_loser_after_the_winner_published_answers_rolled_back(tmp_path):
    store, operation, before, after = await _world(tmp_path)
    loser = await _call(operation, "stage", updates=_update(before))
    await _call(operation, "stage", txn=OTHER, updates=_update(before))
    await _call(operation, "publish", txn=OTHER)
    assert await _call(operation, "rollback", links=loser["members"]) == {"kind": "rollback", "state": "rolled_back"}
    assert await _current(store, before) == after


@pytest.mark.asyncio
async def test_rolling_back_a_loser_before_the_winner_publishes_leaves_the_winner_whole(tmp_path):
    store, operation, before, after = await _world(tmp_path)
    loser = await _call(operation, "stage", updates=_update(before))
    await _call(operation, "stage", txn=OTHER, updates=_update(before))
    assert await _call(operation, "rollback", links=loser["members"]) == {"kind": "rollback", "state": "rolled_back"}
    assert (await _call(operation, "publish", txn=OTHER))["kind"] == "published"
    assert await _current(store, before) == after  # today: CardStorageError current_revision_missing

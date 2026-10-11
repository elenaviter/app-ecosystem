"""W661 contract v6.2: STAGE / PUBLISH / ROLLBACK of Card versions (piece 1, the Hub Card store).

Operator, 10 Oct: "each card has it data ONCE. others LINK"; "never nothing is being scanned"; invitation
"UPSERT. overwrite"; "that error must call callbacl also which will rollback the garbage"; D2: "not yet
final version does not work until theres final arrives. before that olf version works and is active."

Covered: a staged version is invisible until PUBLISH; ROLLBACK leaves nothing; the base fence at STAGE
and at PUBLISH; same-txn retries (no second version, also after a crash before the marker); a lost PUBLISH
reply and a PUBLISH stopped between its pointer and its marker; upsert over an existing and an absent
id; effects once each, effects_pending and retry; no folder is listed; fsync; links only in the marker;
member locks in sorted order; the KEEP_ROLLBACK_MARKER switch.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from connection_hub.delegated_credentials import durable_io
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
from test_card_service import _Cache
from test_card_transaction_store import NOW, SUBJECT_HASH, _setup

TXN = "w661-txn-" + "a" * 32
DIGEST = "d" * 64
WHEN = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW


async def _stage(service, members, *, txn=TXN, digest=DIGEST, at=WHEN, effects=()):
    return await service.stage_card_version(txn=txn, request_digest=digest, catalog="catalog-1", members=members,
                                            now=at, effects=effects)


async def _current(store, card):
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    return None if found is None else found[1]


def _links(answer):
    return [{"subject_hash": m["subject_hash"], "access_id": m["access_id"], "version": m["version"],
             "checksum": m["checksum"]} for m in answer]


def _revision_files(store, card):
    folder = store.card_path(subject_hash=SUBJECT_HASH, access_id=card.access_id) / "revisions"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.mark.asyncio
async def test_a_staged_version_is_invisible_until_publish_then_it_is_current_and_history(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    answer = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    assert answer == [{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "version": 2,
                       "checksum": after.content_hash()}]
    marker = await tx.read_card_version_marker(store, TXN)
    staged = marker["members"][0]["revision_name"]
    # D2: the old version works and is active; the staged one is neither current, history nor readable.
    assert await _current(store, before) == before
    assert staged not in await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert await store.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                     revision_name=staged) is None
    assert await service.publish_card_version(txn=TXN) == answer
    assert await _current(store, before) == after
    assert staged in await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    # D2: once published with every effect recorded, only the version remains: no marker, no sidecar.
    assert not tx.card_version_marker_path(store, TXN).exists()
    assert not any(name.endswith(".card-version.json") for name in _revision_files(store, before))
    record = await store.read_version_record(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                             revision_name=staged)
    base = record.pop("base")
    assert record == {"txn": TXN, "actor": None, "at": WHEN.isoformat(), "catalog": "catalog-1", "binding": None}
    assert base == marker["members"][0]["observed_revision_name"]  # v6.3 item 7: the link to the replaced version
    with pytest.raises(tx.CardTransactionRefused, match="txn_unknown"):  # D5: a lost-reply retry goes to ROLLBACK
        await service.publish_card_version(txn=TXN)
    assert await service.rollback_card_version(txn=TXN, links=_links(answer), at=WHEN) == "already_published"


@pytest.mark.asyncio
async def test_rollback_of_a_staged_txn_leaves_nothing_and_a_later_publish_is_unknown(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    files_before = _revision_files(store, before)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()
    assert await _current(store, before) == before
    with pytest.raises(tx.CardTransactionRefused, match="txn_unknown"):
        await service.publish_card_version(txn=TXN)
    assert await service.rollback_card_version(txn=TXN) == "unknown_txn"
    assert not tx.card_version_marker_path(store, TXN).exists()  # v6.2: an unknown txn writes nothing


@pytest.mark.asyncio
async def test_stage_refuses_a_moved_base_and_publish_refuses_a_card_changed_after_stage(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await _stage(service, [(SUBJECT_HASH, before.access_id, 2, replace(after, card_revision=3))])
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    other = replace(before, card_revision=2, label="another save")
    await service.commit(other, subject_hash=SUBJECT_HASH, expected_revision=1, now=NOW)
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await service.publish_card_version(txn=TXN)
    assert await _current(store, before) == other  # the lost-update fence: the other save stands
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert await _current(store, before) == other


@pytest.mark.asyncio
async def test_a_same_txn_retry_answers_again_without_a_second_version_and_a_different_request_conflicts(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    members = [(SUBJECT_HASH, before.access_id, 1, after)]
    first = await _stage(service, members)
    files = _revision_files(store, before)
    assert await _stage(service, members) == first
    assert _revision_files(store, before) == files
    with pytest.raises(tx.CardTransactionRefused, match="stage_txn_conflict"):
        await _stage(service, members, digest="e" * 64)
    with pytest.raises(tx.CardTransactionRefused, match="stage_txn_conflict"):
        await _stage(service, members, at=WHEN + timedelta(seconds=5))
    await service.publish_card_version(txn=TXN)
    with pytest.raises(tx.CardTransactionRefused, match="txn_closed"):
        await _stage(service, members)


@pytest.mark.asyncio
async def test_a_stage_stopped_after_its_version_file_is_finished_by_the_retry_without_a_second_version(tmp_path, monkeypatch):
    """EMain 16:35Z: "the old resume path that writes again goes": the retry finishes the SAME file."""
    store, service, before, after = await _setup(tmp_path)
    members = [(SUBJECT_HASH, before.access_id, 1, after)]
    real, writes = tx._write_card_version_marker, []

    async def stop_before_staged(store_, marker):
        writes.append(marker["state"])
        if marker["state"] == "staged":
            raise CardStorageError("write_failed")
        await real(store_, marker)
    monkeypatch.setattr(tx, "_write_card_version_marker", stop_before_staged)
    with pytest.raises(CardStorageError):
        await _stage(service, members)
    assert writes == ["staging", "staged"]
    after_crash = _revision_files(store, before)
    monkeypatch.setattr(tx, "_write_card_version_marker", real)
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staging"
    with pytest.raises(tx.CardTransactionRefused, match="txn_not_staged"):
        await service.publish_card_version(txn=TXN)  # STAGE never answered
    await _stage(service, members)
    assert _revision_files(store, before) == after_crash  # the same revision file, nothing new
    await service.publish_card_version(txn=TXN)
    assert len(await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)) == 2


@pytest.mark.asyncio
async def test_a_stage_cancelled_during_its_version_write_is_fully_removed_by_rollback(tmp_path, monkeypatch):
    """Infra K4 (16:45Z) marker-gap schedule: cancellation during the body write, a living PB caller, then
    ROLLBACK. The `staging` marker written first names the file, so nothing of the save remains."""
    store, service, before, after = await _setup(tmp_path)
    files_before = _revision_files(store, before)
    real_write = store.write_revision
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_write(**kwargs):
        entered.set()
        await release.wait()
        return await real_write(**kwargs)
    monkeypatch.setattr(store, "write_revision", slow_write)
    task = asyncio.create_task(_stage(service, [(SUBJECT_HASH, before.access_id, 1, after)]))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staging"
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()
    assert await _current(store, before) == before


@pytest.mark.asyncio
async def test_a_refused_preparation_writes_no_card_file_and_rollback_releases_every_effect(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    files_before = _revision_files(store, before)
    effects = [{"kind": "handle_binding", "key": "k1"}]
    released = []

    async def refuse():
        raise tx.CardTransactionRefused("card_changed")

    async def release(effect, marker):
        released.append((effect["key"], marker["txn"]))
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog={"version": "v1", "content_hash": "h"},
                                         members=[(SUBJECT_HASH, before.access_id, 1, after)], now=WHEN,
                                         effects=effects, prepare=refuse)
    assert _revision_files(store, before) == files_before
    assert await service.rollback_card_version(txn=TXN, release=release) == "rolled_back"
    assert released == [("k1", TXN)]
    assert not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
async def test_rollback_after_a_lost_publish_reply_answers_already_published_and_keeps_the_version(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    answer = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    await service.publish_card_version(txn=TXN)
    assert await service.rollback_card_version(txn=TXN, links=_links(answer), at=WHEN) == "already_published"
    assert await service.rollback_card_version(txn=TXN) == "unknown_txn"  # without its links: nothing to name
    assert await service.rollback_card_version(txn=TXN, links=_links(answer),
                                               at=WHEN + timedelta(seconds=1)) == "unknown_txn"
    assert await _current(store, before) == after


@pytest.mark.asyncio
async def test_a_publish_stopped_between_pointer_and_marker_is_published_by_rollback_not_deleted(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    marker = await tx.read_card_version_marker(store, TXN)
    from connection_hub.delegated_credentials.cards.model import CardCurrentPointer
    await store.advance_current(subject_hash=SUBJECT_HASH,
                                pointer=CardCurrentPointer.from_mapping(marker["members"][0]["pointer"]))
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staged"
    assert await service.rollback_card_version(txn=TXN) == "already_published"
    assert not tx.card_version_marker_path(store, TXN).exists()  # finished, so only the version remains
    assert await _current(store, before) == after


@pytest.mark.asyncio
async def test_invitation_upsert_overwrites_an_existing_id_and_creates_an_absent_one(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    await _stage(service, [(SUBJECT_HASH, before.access_id, None, after)])  # existing at r1: takes r2
    await service.publish_card_version(txn=TXN)
    assert await _current(store, before) == after
    fresh = replace(before, access_id="aut_new_invite", card_revision=1, label="invited")
    await _stage(service, [(SUBJECT_HASH, fresh.access_id, None, fresh)], txn="w661-txn-" + "b" * 32)
    await service.publish_card_version(txn="w661-txn-" + "b" * 32)
    assert await _current(store, fresh) == fresh


@pytest.mark.asyncio
async def test_invite_remove_invite_overwrites_the_same_id_without_any_history_scan(tmp_path):
    """Operator: the card id "NEVER changes"; invitation "UPSERT. overwrite"; no "slot used" refusal."""
    store, service, before, after = await _setup(tmp_path)
    store.current_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).unlink()  # removed
    again = replace(before, card_revision=1, label="invited again")
    await _stage(service, [(SUBJECT_HASH, before.access_id, None, again)])
    await service.publish_card_version(txn=TXN)
    assert await _current(store, before) == again


@pytest.mark.asyncio
async def test_effects_run_once_each_pending_until_done_and_never_again(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    effects = [{"kind": "handle_binding", "key": "k1"}, {"kind": "handle_binding", "key": "k2"}]
    answer = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], effects=effects)
    runs = []

    async def flaky(effect, marker):
        runs.append(effect["key"])
        if effect["key"] == "k2" and runs.count("k2") == 1:
            raise RuntimeError("binding store unavailable")
        return "applied"
    with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
        await service.publish_card_version(txn=TXN)  # no executor: the flip is done, effects are pending
    assert await _current(store, before) == after
    with pytest.raises(RuntimeError):
        await service.publish_card_version(txn=TXN, run_effect=flaky)
    assert runs == ["k1", "k2"]
    assert (await tx.read_card_version_marker(store, TXN))["effect_outcomes"] == {"0": "applied"}
    await service.publish_card_version(txn=TXN, run_effect=flaky)
    assert runs == ["k1", "k2", "k2"]
    assert not tx.card_version_marker_path(store, TXN).exists()  # every effect recorded: the marker went
    assert await service.rollback_card_version(txn=TXN, run_effect=flaky, links=_links(answer),
                                               at=WHEN) == "already_published"
    assert runs == ["k1", "k2", "k2"]


@pytest.mark.asyncio
async def test_no_operation_lists_any_folder(tmp_path, monkeypatch):
    """Operator: "never nothing is being scanned"; "nothing should grow with the cards". PUBLISH's pointer guard
    reads each Card's own in-flight file instead of listing the in-flight queues (EMain 16:57Z)."""
    store, service, before, after = await _setup(tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("a W661 operation listed a folder")
    monkeypatch.setattr(durable_io, "_list_children", refuse)
    monkeypatch.setattr(pathlib.Path, "iterdir", refuse)
    monkeypatch.setattr(pathlib.Path, "glob", refuse)
    monkeypatch.setattr(os, "listdir", refuse)
    monkeypatch.setattr(os, "scandir", refuse)
    second = "w661-txn-" + "c" * 32
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    await _stage(service, [(SUBJECT_HASH, "aut_other", None, replace(after, access_id="aut_other", card_revision=1))],
                 txn=second)
    assert await service.rollback_card_version(txn=second) == "rolled_back"
    answer = await service.publish_card_version(txn=TXN)
    assert await service.rollback_card_version(txn=TXN, links=_links(answer), at=WHEN) == "already_published"


@pytest.mark.asyncio
async def test_every_write_is_fsynced_and_the_marker_holds_links_only(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    synced = []
    real = os.fsync
    monkeypatch.setattr(durable_io.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    assert len(synced) >= 6  # revision marker, revision, txn marker: each file and its directory
    raw = tx.card_version_marker_path(store, TXN).read_text(encoding="utf-8")
    marker = json.loads(raw)
    assert set(marker) == {"schema", "txn", "state", "request_digest", "request_id", "actor", "binding", "at",
                           "catalog", "members", "reads", "effects", "effect_outcomes"}
    assert set(marker["members"][0]) == {"subject_hash", "access_id", "base_version", "observed_version",
                                         "observed_revision_name", "version", "revision_name", "content_hash",
                                         "pointer"}
    for card_field in ("staged change", before.grantor_subject, "named_service_operations", "issuer_ref"):
        assert card_field not in raw  # "each card has it data ONCE. others LINK"


@pytest.mark.asyncio
async def test_member_locks_are_taken_in_sorted_order_and_held_for_every_operation(tmp_path):
    taken, held = [], set()

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        assert resource_id not in held
        taken.append(resource_id)
        held.add(resource_id)
        try:
            yield
        finally:
            held.discard(resource_id)

    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    _, _, before, _ = await _setup(tmp_path / "seed")
    cards = [replace(before, access_id=f"aut_{name}", card_revision=1) for name in ("zz", "aa", "mm")]
    await _stage(service, [(SUBJECT_HASH, c.access_id, None, c) for c in cards])
    assert taken == ["delegated-card:aut_aa", "delegated-card:aut_mm", "delegated-card:aut_zz"]
    taken.clear()
    await service.publish_card_version(txn=TXN)
    # read members, take their locks in order, re-read under the locks
    assert taken == ["delegated-card:aut_aa", "delegated-card:aut_mm", "delegated-card:aut_zz"]
    assert held == set()


@pytest.mark.asyncio
async def test_stage_and_rollback_of_one_txn_serialize_on_the_card_lock(tmp_path):
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        lock = locks.setdefault(str(lock_path), asyncio.Lock())
        async with lock:
            await asyncio.sleep(0)
            yield

    store, service, before, after = await _setup(tmp_path, mutation_lock=mutation_lock)
    staged, answer = await asyncio.gather(_stage(service, [(SUBJECT_HASH, before.access_id, 1, after)]),
                                          service.rollback_card_version(txn=TXN))
    marker = await tx.read_card_version_marker(store, TXN)
    if answer == "unknown_txn":  # ROLLBACK first: STAGE then stands, staged and inert (R1)
        assert marker["state"] == "staged"
    else:  # STAGE first: ROLLBACK removed all of it
        assert answer == "rolled_back" and marker is None
    assert await _current(store, before) == before


@pytest.mark.asyncio
async def test_the_keep_rollback_marker_switch_closes_the_txn_for_a_late_call(tmp_path, monkeypatch):
    """EMain 16:35Z: a one-line switch until Infra's K4 probe decides; off (v6.2) by default."""
    assert tx.KEEP_ROLLBACK_MARKER is False
    monkeypatch.setattr(tx, "KEEP_ROLLBACK_MARKER", True)
    store, service, before, after = await _setup(tmp_path)
    members = [(SUBJECT_HASH, before.access_id, 1, after)]
    await _stage(service, members)
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "rolled_back"
    with pytest.raises(tx.CardTransactionRefused, match="txn_closed"):
        await _stage(service, members)
    with pytest.raises(tx.CardTransactionRefused, match="txn_closed"):
        await service.publish_card_version(txn=TXN)
    late = "w661-txn-" + "f" * 32
    assert await service.rollback_card_version(txn=late) == "unknown_txn"
    with pytest.raises(tx.CardTransactionRefused, match="txn_closed"):
        await _stage(service, members, txn=late)


@pytest.mark.asyncio
async def test_a_request_without_its_own_time_or_with_a_bad_txn_is_refused(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_version_request_invalid"):
        await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], at=datetime(2026, 10, 10))
    with pytest.raises(tx.CardTransactionRefused, match="card_version_txn_invalid"):
        await service.publish_card_version(txn="../escape")


class _PortRefused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@pytest.mark.asyncio
async def test_the_piece_2_port_answers_links_hands_effects_the_port_marker_and_maps_refusals(tmp_path):
    from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore

    store, service, before, after = await _setup(tmp_path)

    class _Catalog:
        active = {"version": "v1", "content_hash": "c" * 64}

        async def read_active(self):
            return type("Doc", (), self.active)()
    catalog = _Catalog()
    with pytest.raises(_PortRefused) as refused:  # D1: no catalog store bound, STAGE fails closed
        await ServiceCardVersionStore(service, refused=_PortRefused).stage(
            TXN, request_id="r", request_digest=DIGEST, catalog={}, actor_subject="p", actor_kind="caller",
            members=[], effects=[], prepare=None, at=WHEN, scope="s", caller="c")
    assert refused.value.code == "storage_unavailable"
    port = ServiceCardVersionStore(service, refused=_PortRefused, catalog_store=catalog)
    member = {"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "base_version": 1, "value": after.to_dict()}
    effects = [{"kind": "handle_binding", "key": "k1", "access_id": before.access_id, "payload": {"card_revision": 2}}]
    seen, prepared = [], []

    async def prepare():
        prepared.append(True)

    async def apply(effect, marker):
        seen.append(marker)
        current = await port.read_current(SUBJECT_HASH, before.access_id)
        assert current == {"version": 2, "checksum": after.content_hash()}  # the flip happened before effects
        return "applied"

    link = {"card": {"subject_hash": SUBJECT_HASH, "access_id": before.access_id}, "version": 2,
            "checksum": after.content_hash()}
    stage = dict(request_id="r-1", request_digest=DIGEST, catalog={"version": "v1", "content_hash": "c" * 64},
                 actor_subject="person-1", actor_kind="caller", members=[member], effects=effects, prepare=prepare,
                 at=WHEN, scope="project-a", caller="pb-service")
    bound = {"scope": "project-a", "caller": "pb-service"}
    assert await port.stage(TXN, **stage) == {"members": [link]}
    assert prepared == [True]
    with pytest.raises(_PortRefused) as refused:
        await port.publish(TXN, scope="project-b", caller="pb-service", apply=apply)  # another entitled scope
    assert refused.value.code == "txn_scope_mismatch"
    with pytest.raises(_PortRefused) as refused:
        await port.rollback(TXN, scope="project-a", caller="other-service", apply=apply, release=apply)
    assert refused.value.code == "txn_scope_mismatch"
    assert await port.read_current(SUBJECT_HASH, before.access_id) == {"version": 1, "checksum": before.content_hash()}
    assert await port.publish(TXN, **bound, apply=apply) == {"members": [link]}
    assert seen == [{"txn": TXN, "members": [{**link, "base_version": 1}]}]
    assert await port.rollback(TXN, **bound, links=[link], at=WHEN, apply=apply,
                               release=apply) == {"state": "already_published"}
    with pytest.raises(_PortRefused) as refused:  # the version record carries the binding too
        await port.rollback(TXN, scope="project-b", caller="pb-service", links=[link], at=WHEN, apply=apply,
                            release=apply)
    assert refused.value.code == "txn_scope_mismatch"
    assert len(seen) == 1  # effects ran once
    with pytest.raises(_PortRefused) as refused:
        await port.stage("w661-txn-" + "e" * 32, **{**stage, "members": [member]})
    assert refused.value.code == "card_changed"
    with pytest.raises(_PortRefused) as refused:
        await port.publish("w661-txn-" + "9" * 32, **bound, apply=apply)
    assert refused.value.code == "txn_unknown"
    assert await port.rollback("w661-txn-" + "9" * 32, **bound, apply=apply, release=apply) == {"state": "unknown_txn"}
    with pytest.raises(_PortRefused) as refused:
        await port.stage("w661-txn-" + "8" * 32, **{**stage, "members": [{**member, "value": {"bad": 1}}]})
    assert refused.value.code == "edit_invalid"
    catalog.active = {"version": "v2", "content_hash": "d" * 64}  # a catalog publish since PB's read
    second = replace(after, card_revision=3, label="third")
    with pytest.raises(_PortRefused) as refused:
        await port.stage("w661-txn-" + "7" * 32, **{**stage, "members": [{**member, "base_version": 2,
                                                                         "value": second.to_dict()}]})
    assert refused.value.code == "stage_catalog_moved"


@pytest.mark.asyncio
async def test_a_cancelled_stage_drains_its_thread_write_before_rollback_can_run_and_nothing_remains(tmp_path, monkeypatch):
    """EMain 16:45Z amendment, item 3 (Infra K4): "Cancelling to_thread does not stop its syscall". The paused
    body write is a real worker-thread write; ROLLBACK waits on the Card lock until it has finished."""
    import threading

    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        async with locks.setdefault(str(lock_path), asyncio.Lock()):
            yield

    store, service, before, after = await _setup(tmp_path, mutation_lock=mutation_lock)
    files_before = _revision_files(store, before)
    real_write = durable_io._write_text_atomic
    entered, resume, order = threading.Event(), threading.Event(), []

    def paused_write(path, text):
        if path.parent.name == "revisions" and path.name.startswith("card_revision_"):
            entered.set()
            resume.wait(10)
            real_write(path, text)
            order.append("write_finished")
            return
        real_write(path, text)
    monkeypatch.setattr(durable_io, "_write_text_atomic", paused_write)
    stage = asyncio.create_task(_stage(service, [(SUBJECT_HASH, before.access_id, 1, after)]))
    await asyncio.to_thread(entered.wait, 10)
    stage.cancel()

    async def rollback():
        answer = await service.rollback_card_version(txn=TXN)
        order.append("rollback_done")
        return answer
    rolling = asyncio.create_task(rollback())
    await asyncio.sleep(0.2)
    assert not rolling.done() and order == []  # the Card lock is held until the started write returns
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await stage
    assert await rolling == "rolled_back"
    assert order == ["write_finished", "rollback_done"]
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
async def test_a_same_txn_stage_under_another_binding_is_refused_and_touches_nothing(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    members = [(SUBJECT_HASH, before.access_id, 1, after)]
    a, b = {"scope": "project-a", "caller": "pb"}, {"scope": "project-b", "caller": "pb"}
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", members=members, now=WHEN, binding=a)
    marker = tx.card_version_marker_path(store, TXN).read_bytes()
    for call in (service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", members=members, now=WHEN,
                                            binding=b),
                 service.publish_card_version(txn=TXN, binding=b), service.rollback_card_version(txn=TXN, binding=b),
                 service.publish_card_version(txn=TXN)):
        with pytest.raises(tx.CardTransactionRefused, match="txn_scope_mismatch"):
            await call
    assert tx.card_version_marker_path(store, TXN).read_bytes() == marker
    assert await _current(store, before) == before


def _locked_service(store):
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        async with locks.setdefault(str(lock_path), asyncio.Lock()):
            yield
    # A separate worker composes a fresh store object over the same durable root.
    reopened = BundleStorageDelegatedCardStore(store.root.parent.parent,
                                               lifecycle_lock_scope=store.lifecycle_lock_scope)
    tx.bind_transaction_decisions(reopened, store._card_transaction_decisions)
    return DelegatedCardService(store=reopened, cache=_Cache(), mutation_lock=mutation_lock)


@pytest.mark.asyncio
async def test_a_stage_cancelled_during_prepare_finishes_the_preparation_before_rollback_releases_it(tmp_path):
    """EMain 17:00Z (Infra K4): prepare runs through cancellation_safe_await, so a cancelled STAGE cannot leave a
    preparation running after the Card locks are released; ROLLBACK then releases it and removes everything."""
    store, _, before, after = await _setup(tmp_path)
    service = _locked_service(store)
    files_before = _revision_files(store, before)
    entered, resume, order = asyncio.Event(), asyncio.Event(), []

    async def prepare():
        entered.set()
        await resume.wait()
        order.append("prepared")

    async def release(effect, marker):
        order.append("released")
    stage = asyncio.create_task(service.stage_card_version(
        txn=TXN, request_digest=DIGEST, catalog="c", members=[(SUBJECT_HASH, before.access_id, 1, after)], now=WHEN,
        effects=[{"kind": "handle_binding", "key": "k1"}], prepare=prepare))
    await entered.wait()
    stage.cancel()
    rolling = asyncio.create_task(service.rollback_card_version(txn=TXN, release=release))
    await asyncio.sleep(0.05)
    assert not rolling.done() and order == []
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await stage
    assert await rolling == "rolled_back"
    assert order == ["prepared", "released"]
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
async def test_a_rollback_cancelled_during_release_leaves_its_marker_and_the_retry_finishes(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    service = _locked_service(store)
    files_before = _revision_files(store, before)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], effects=[{"kind": "handle_binding", "key": "k1"}])
    entered, resume, released = asyncio.Event(), asyncio.Event(), []

    async def slow_release(effect, marker):
        entered.set()
        await resume.wait()
        released.append(effect["key"])
    rolling = asyncio.create_task(service.rollback_card_version(txn=TXN, release=slow_release))
    await entered.wait()
    rolling.cancel()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await rolling
    assert released == ["k1"]  # the started release finished; the locks were held until it did
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staged"  # the marker still locates the files

    async def release(effect, marker):
        released.append(effect["key"])  # piece 2's release is a no-op for an already discarded re-wrap
    assert await service.rollback_card_version(txn=TXN, release=release) == "rolled_back"
    assert released == ["k1", "k1"]
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
async def test_a_partial_deletion_keeps_the_marker_until_a_retry_removes_the_rest(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    files_before = _revision_files(store, before)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    real_unlink, calls = pathlib.Path.unlink, []

    def failing_unlink(self, missing_ok=False):
        calls.append(self.name)
        if len(calls) == 2:
            raise PermissionError("share refused the delete")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(pathlib.Path, "unlink", failing_unlink)
    with pytest.raises(CardStorageError, match="card_version_rollback_failed"):
        await service.rollback_card_version(txn=TXN)
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert tx.card_version_marker_path(store, TXN).exists()  # the marker goes last, so it still names the rest
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert _revision_files(store, before) == files_before
    assert not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, code", [("issuer_update", "issuer_update_preparation_unresolved"),
                                        ("lifecycle", "lifecycle_preparation_unresolved")])
async def test_a_hub_local_operation_in_flight_on_the_card_refuses_publish_by_one_direct_read(tmp_path, monkeypatch,
                                                                                           kind, code):
    """EMain 16:57Z: the Card's own inflight.json replaces the queue listing; the refusal codes stay."""
    from connection_hub.delegated_credentials.cards import inflight, lifecycle_store, update_store

    store, service, before, after = await _setup(tmp_path)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    state = {"state": "prepared", "serving_state": "pending"}

    async def receipt(store_, txn):
        return dict(state) if txn == "other-op" else None
    monkeypatch.setattr(update_store if kind == "issuer_update" else lifecycle_store, "read_receipt", receipt)
    await inflight.claim_inflight(store, subject_hash=SUBJECT_HASH, access_id=before.access_id, kind=kind,
                                  txn="other-op")
    with pytest.raises(CardStorageError, match=code):
        await service.publish_card_version(txn=TXN)
    with pytest.raises(CardStorageError, match=code):  # a second Hub-local operation is refused too
        await inflight.claim_inflight(store, subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                      kind="issuer_update", txn="third-op")
    assert await _current(store, before) == before
    state.update(state="committed", serving_state="complete")  # it finished; its cleanup was interrupted
    await service.publish_card_version(txn=TXN)  # a stale file is not honoured
    assert await _current(store, before) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", ["stage", "commit"])
async def test_a_writer_finalizes_a_stopped_publish_so_its_rollback_never_deletes_published_history(tmp_path,
                                                                                                    monkeypatch,
                                                                                                    successor):
    """D3 (EMain; Infra's K2 cut, successor_before_recovery): A writes current.json, its `published` marker write
    fails, the lock is released. The next writer of that Card first records A as published, then builds v3; A's
    later ROLLBACK answers already_published and A's version stays as history."""
    store, service, before, after = await _setup(tmp_path)
    answer = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    staged = (await tx.read_card_version_marker(store, TXN))["members"][0]["revision_name"]
    real = tx._write_card_version_marker

    async def fail_published(store_, marker):
        if marker["state"] == "published":
            raise CardStorageError("write_failed")
        await real(store_, marker)
    monkeypatch.setattr(tx, "_write_card_version_marker", fail_published)
    with pytest.raises(CardStorageError):
        await service.publish_card_version(txn=TXN)
    monkeypatch.setattr(tx, "_write_card_version_marker", real)
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staged"
    v3 = replace(after, card_revision=3, label="successor")
    if successor == "stage":
        await _stage(service, [(SUBJECT_HASH, before.access_id, 2, v3)], txn="w661-txn-" + "d" * 32)
        await service.publish_card_version(txn="w661-txn-" + "d" * 32)
    else:
        await service.commit(v3, subject_hash=SUBJECT_HASH, expected_revision=2, now=NOW)
    assert await _current(store, before) == v3
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "published"  # finalized, left for A
    assert await service.rollback_card_version(txn=TXN) == "already_published"  # even without its links
    assert not tx.card_version_marker_path(store, TXN).exists()  # A's own ROLLBACK removed it
    assert await service.rollback_card_version(txn=TXN, links=_links(answer), at=WHEN) == "already_published"
    assert staged in await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert await store.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                     revision_name=staged) == after


@pytest.mark.asyncio
async def test_a_staging_retry_completes_the_preparation_before_answering_staged(tmp_path):
    """Infra 17:2xZ red 1: a `staging` marker is no evidence that preparation succeeded."""
    store, service, before, after = await _setup(tmp_path)
    attempts = []

    async def prepare():
        attempts.append("prepare")
        if len(attempts) == 1:
            raise RuntimeError("preparation unavailable")
    args = dict(txn=TXN, request_digest=DIGEST, catalog="c", members=[(SUBJECT_HASH, before.access_id, 1, after)],
                now=WHEN, effects=[{"kind": "handle_binding", "key": "k1"}], prepare=prepare)
    with pytest.raises(RuntimeError):
        await service.stage_card_version(**args)
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staging"
    await service.stage_card_version(**args)
    assert attempts == ["prepare", "prepare"]
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staged"


@pytest.mark.asyncio
async def test_a_reader_sees_the_new_version_once_current_names_it_even_before_the_published_marker(tmp_path):
    """Infra 17:2xZ red 3: the pointer-before-marker cut of a real PUBLISH is not a missing revision."""
    from connection_hub.delegated_credentials.cards.model import CardCurrentPointer

    store, service, before, after = await _setup(tmp_path)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    marker = await tx.read_card_version_marker(store, TXN)
    assert await _current(store, before) == before
    await store.advance_current(subject_hash=SUBJECT_HASH,
                                pointer=CardCurrentPointer.from_mapping(marker["members"][0]["pointer"]))
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "staged"
    assert await _current(store, before) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", ["loser_rolls_back_first", "loser_rolls_back_after", "loser_publishes_after"])
async def test_two_saves_with_the_same_version_content_and_time_never_share_a_version_file(tmp_path, schedule):
    """B1 (Spark App 17:29Z; EMain 17:30Z): the file name carries the txn, so one file belongs to one txn."""
    store, service, before, after = await _setup(tmp_path)
    loser, winner = TXN, "w661-txn-" + "e" * 32
    a = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], txn=loser)
    b = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], txn=winner)
    names = [(await tx.read_card_version_marker(store, t))["members"][0]["revision_name"] for t in (loser, winner)]
    assert names[0] != names[1] and a == b  # the same link {card, version, checksum}, two files
    if schedule == "loser_rolls_back_first":
        assert await service.rollback_card_version(txn=loser) == "rolled_back"
        await service.publish_card_version(txn=winner)
    else:
        await service.publish_card_version(txn=winner)
        if schedule == "loser_rolls_back_after":
            assert await service.rollback_card_version(txn=loser) == "rolled_back"  # never already_published
            assert await service.rollback_card_version(txn=loser, links=_links(a), at=WHEN) == "unknown_txn"
        else:
            with pytest.raises(tx.CardTransactionRefused, match="card_changed"):  # the lost-update fence holds
                await service.publish_card_version(txn=loser)
    assert await _current(store, before) == after  # the winner's version is readable
    assert names[1] in await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert await service.rollback_card_version(txn=winner, links=_links(b), at=WHEN) == "already_published"


@pytest.mark.asyncio
async def test_a_successor_is_refused_while_its_predecessor_has_unrecorded_effects(tmp_path):
    """Infra 17:37Z (D3, "complete or refuse"): A's pointer is published but its handle_binding effect is not
    recorded. A STAGE (no executor) or pointer writer is refused; a PUBLISH holding the executor finishes A first."""
    store, service, before, after = await _setup(tmp_path)
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], effects=[{"kind": "handle_binding", "key": "k1"}])
    with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
        await service.publish_card_version(txn=TXN)
    v3 = replace(after, card_revision=3, label="successor")
    b = "w661-txn-" + "b" * 32
    with pytest.raises(CardStorageError, match="card_version_effects_pending"):
        await _stage(service, [(SUBJECT_HASH, before.access_id, 2, v3)], txn=b)
    with pytest.raises(CardStorageError, match="card_version_effects_pending"):
        await service.commit(v3, subject_hash=SUBJECT_HASH, expected_revision=2, now=NOW)
    ran = []

    async def apply(effect, marker):
        ran.append((marker["txn"], effect["key"]))
        return "applied"
    assert await service.rollback_card_version(txn=TXN, run_effect=apply) == "already_published"
    assert ran == [(TXN, "k1")]
    await _stage(service, [(SUBJECT_HASH, before.access_id, 2, v3)], txn=b)
    await service.publish_card_version(txn=b, run_effect=apply)
    assert await _current(store, before) == v3


@pytest.mark.asyncio
async def test_a_publish_holding_an_executor_still_refuses_and_never_runs_another_txns_effects(tmp_path):
    """EMain 17:43Z: only a txn's own PUBLISH retry or ROLLBACK runs its effects (W693 Q4 is open)."""
    store, service, before, after = await _setup(tmp_path)
    b = "w661-txn-" + "b" * 32
    other = replace(after, label="B's edit")
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], effects=[{"kind": "handle_binding", "key": "a"}])
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, other)], txn=b, effects=[{"kind": "handle_binding",
                                                                                       "key": "b"}])
    with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
        await service.publish_card_version(txn=TXN)  # A's pointer moved; its effect is not recorded
    ran = []

    async def apply(effect, marker):
        ran.append((marker["txn"], effect["key"]))
        return "applied"
    with pytest.raises(CardStorageError, match="card_version_effects_pending"):
        await service.publish_card_version(txn=b, run_effect=apply)
    assert ran == []  # B never ran A's effect
    assert (await tx.read_card_version_marker(store, TXN))["effect_outcomes"] == {}
    assert await service.rollback_card_version(txn=TXN, run_effect=apply) == "already_published"
    assert ran == [(TXN, "a")]
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):  # then B meets the ordinary fence
        await service.publish_card_version(txn=b, run_effect=apply)


@pytest.mark.asyncio
async def test_the_port_answers_a_pending_predecessor_with_the_contract_code_effects_pending(tmp_path):
    """Spark App M1 (17:44Z): finalize's refusal reaches PB as effects_pending, not storage_unavailable."""
    from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore

    store, service, before, after = await _setup(tmp_path)

    class _Catalog:
        async def read_active(self):
            return type("Doc", (), {"version": "v1", "content_hash": "c" * 64})()
    port = ServiceCardVersionStore(service, refused=_PortRefused, catalog_store=_Catalog())
    await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)], effects=[{"kind": "handle_binding", "key": "a"}])
    with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
        await service.publish_card_version(txn=TXN)
    v3 = replace(after, card_revision=3, label="successor")
    with pytest.raises(_PortRefused) as refused:
        await port.stage("w661-txn-" + "b" * 32, request_id="r", request_digest=DIGEST,
                         catalog={"version": "v1", "content_hash": "c" * 64}, actor_subject="p", actor_kind="caller",
                         members=[{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "base_version": 2,
                                   "value": v3.to_dict()}], effects=[], prepare=None, at=WHEN, scope="s", caller="c")
    assert refused.value.code == "effects_pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["checksum", "version"])
async def test_a_rollback_link_must_match_the_exact_version_in_its_file(tmp_path, field):
    """Infra 17:52Z: the file name carries checksum[:12]; a link differing in the rest of the checksum (or the
    version) is never confirmed already_published. It fails closed."""
    store, service, before, after = await _setup(tmp_path)
    answer = await _stage(service, [(SUBJECT_HASH, before.access_id, 1, after)])
    await service.publish_card_version(txn=TXN)
    link = _links(answer)[0]
    if field == "checksum":
        link["checksum"] = link["checksum"][:-1] + ("0" if link["checksum"][-1] != "0" else "1")
    else:
        link["version"] = 3
    if field == "checksum":
        with pytest.raises(tx.CardTransactionRefused, match="card_version_link_mismatch"):
            await service.rollback_card_version(txn=TXN, links=[link], at=WHEN)
    else:  # another version names another file: nothing of this txn there
        assert await service.rollback_card_version(txn=TXN, links=[link], at=WHEN) == "unknown_txn"
    assert await service.rollback_card_version(txn=TXN, links=_links(answer), at=WHEN) == "already_published"
    assert await _current(store, before) == after


async def _control_card(service, before):
    """A second Card of the same person, standing in for the Control C that My Reset reads."""
    control = replace(before, access_id="aut_control_c", card_revision=1, label="control C")
    await service.commit(control, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    return control


@pytest.mark.asyncio
async def test_a_read_member_that_moved_before_stage_refuses_card_changed_and_writes_nothing(tmp_path):
    """EMain 18:10Z: My Reset reads C under C's lock; a C changed between the plan and the lock refuses."""
    store, service, before, after = await _setup(tmp_path)
    control = await _control_card(service, before)
    await service.commit(replace(control, card_revision=2, label="C moved"), subject_hash=SUBJECT_HASH,
                         expected_revision=1, now=NOW)
    files = _revision_files(store, before)
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                         members=[(SUBJECT_HASH, before.access_id, 1, after)],
                                         reads=[(SUBJECT_HASH, control.access_id, 1)])
    assert _revision_files(store, before) == files and not tx.card_version_marker_path(store, TXN).exists()


@pytest.mark.asyncio
async def test_a_read_member_that_moves_between_stage_and_publish_refuses_publish(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    control = await _control_card(service, before)
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)],
                                     reads=[(SUBJECT_HASH, control.access_id, 1)])
    assert (await tx.read_card_version_marker(store, TXN))["reads"] == [
        {"subject_hash": SUBJECT_HASH, "access_id": control.access_id, "version": 1}]  # links only
    await service.commit(replace(control, card_revision=2, label="C moved"), subject_hash=SUBJECT_HASH,
                         expected_revision=1, now=NOW)
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await service.publish_card_version(txn=TXN)
    assert await _current(store, before) == before
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"


@pytest.mark.asyncio
async def test_read_members_are_locked_in_the_one_sorted_order_with_the_written_members(tmp_path):
    taken = []

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        taken.append(resource_id)
        yield

    store, service, before, after = await _setup(tmp_path, mutation_lock=mutation_lock)
    control = replace(before, access_id="aut_aa_control", card_revision=1)
    await service.commit(control, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    taken.clear()  # measure the operation, not fixture seeding
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="c", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)],
                                     reads=[(SUBJECT_HASH, control.access_id, 1)])
    assert taken == sorted(taken) and set(taken) == {f"delegated-card:{control.access_id}",
                                                     f"delegated-card:{before.access_id}"}
    taken.clear()
    await service.publish_card_version(txn=TXN)
    assert set(taken) == {f"delegated-card:{control.access_id}", f"delegated-card:{before.access_id}"}


@pytest.mark.asyncio
async def test_a_read_member_is_never_written_and_answers_no_link(tmp_path):
    from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore

    store, service, before, after = await _setup(tmp_path)
    control = await _control_card(service, before)
    control_files = _revision_files(store, control)
    control_pointer = store.current_path(subject_hash=SUBJECT_HASH, access_id=control.access_id).read_bytes()

    class _Catalog:
        async def read_active(self):
            return type("Doc", (), {"version": "v1", "content_hash": "c" * 64})()
    port = ServiceCardVersionStore(service, refused=_PortRefused, catalog_store=_Catalog())
    answer = await port.stage(TXN, request_id="r", request_digest=DIGEST,
                              catalog={"version": "v1", "content_hash": "c" * 64}, actor_subject="p",
                              actor_kind="caller", members=[{"subject_hash": SUBJECT_HASH, "access_id": before.access_id,
                                                             "base_version": 1, "value": after.to_dict()}],
                              effects=[], prepare=None, at=WHEN, scope="s", caller="c",
                              reads=[{"card": {"subject_hash": SUBJECT_HASH, "access_id": control.access_id},
                                      "version": 1}])
    assert [m["card"]["access_id"] for m in answer["members"]] == [before.access_id]  # no link for C
    await port.publish(TXN, scope="s", caller="c", apply=None)
    assert await _current(store, before) == after
    assert _revision_files(store, control) == control_files
    assert store.current_path(subject_hash=SUBJECT_HASH, access_id=control.access_id).read_bytes() == control_pointer
    with pytest.raises(tx.CardTransactionRefused, match="card_version_members_invalid"):  # read and written at once
        await service.stage_card_version(txn="w661-txn-" + "f" * 32, request_digest=DIGEST, catalog="c", now=WHEN,
                                         members=[(SUBJECT_HASH, control.access_id, 1, replace(control, card_revision=2))],
                                         reads=[(SUBJECT_HASH, control.access_id, 1)])

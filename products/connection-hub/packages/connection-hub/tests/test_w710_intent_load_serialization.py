"""Controlled portable recorder/PREPARE ordering; cache and exclusion are declared fixture seams.

The real participant, manifest and version files run unchanged. This is not
SDK Redis/PG or live serving proof; the composed PB gate supplies those inputs.
"""
import asyncio
from contextlib import asynccontextmanager
import contextvars

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from connection_hub.delegated_credentials.cards import intent_links
from connection_hub.delegated_credentials.cards import card_participant, transaction_store
from connection_hub.delegated_credentials.durable_io import read_json_or_none, write_json_atomic
from test_w697_intent_links import TXID, _unrecorded, _unrecorded_group


async def _cancel_tasks(*tasks):
    present = [task for task in tasks if task is not None]
    for task in present:
        if not task.done():
            task.cancel()
    await asyncio.gather(*present, return_exceptions=True)


def _controlled_sections(service):
    """Keep the service's section path; supply deterministic per-resource exclusion only in the fixture."""
    locks, waiting, keys = {}, asyncio.Event(), []
    original = service._mutation_lock

    @asynccontextmanager
    async def lock(**kwargs):
        key = str(kwargs["lock_path"])
        if asyncio.current_task().get_name() == "overlap-reader":
            keys.append(kwargs["resource_id"])
            waiting.set()
        async with locks.setdefault(key, asyncio.Lock()):
            async with original(**kwargs):
                yield

    service._mutation_lock = lock
    return waiting, keys


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True], ids=["single", "group"])
@pytest.mark.parametrize("prepare", [False, True], ids=["load", "prepare"])
async def test_loader_waits_for_same_member_recorder_then_uses_ready_manifest(tmp_path, monkeypatch, group, prepare):
    setup = _unrecorded_group if group else _unrecorded
    store, service, source, intent, hub, _ = await setup(tmp_path)
    waiting, keys = _controlled_sections(service)
    staging, release = asyncio.Event(), asyncio.Event()
    actual_write = intent_links.write_hidden_version

    async def pause_write(*args, **kwargs):
        staging.set()  # The complete staging manifest is already durable; member sections stay held.
        await release.wait()
        return await actual_write(*args, **kwargs)

    monkeypatch.setattr(intent_links, "write_hidden_version", pause_write)
    writer = asyncio.create_task(source.record(intent), name="overlap-recorder")
    reader = signal = None
    try:
        await asyncio.wait_for(staging.wait(), 3)
        before = await read_json_or_none(source._path(TXID))
        assert before["status"] == "staging"
        reader = asyncio.create_task(hub.prepare(TXID) if prepare else source.load(TXID), name="overlap-reader")
        signal = asyncio.create_task(waiting.wait())
        done, _ = await asyncio.wait((reader, signal), timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert signal in done and not reader.done(), (
            "reader bypassed the recorder's sections", reader.exception() if reader.done() else None)
        release.set()
        await asyncio.wait_for(writer, 3)
        result = await asyncio.wait_for(reader, 3)
        after = await read_json_or_none(source._path(TXID))
        assert after == {**before, "status": "ready"}
        expected = before["members"] if group else [before]
        assert keys[:len(expected)] == ["delegated-card:" + member["access_id"] for member in expected]
        if prepare:
            assert result.transaction_id == TXID
            assert result.global_intent_digest == intent.intent_digest
        elif group:
            assert [(m.original, m.candidate, m.action) for m in result.members] == [
                (m.original, m.candidate, m.action) for m in intent.members]
        else:
            assert result.original == intent.original and result.candidate == intent.candidate
        assert store.current_path(subject_hash=expected[0]["subject_hash"], access_id=expected[0]["access_id"]).exists()
    finally:
        release.set()
        await _cancel_tasks(writer, reader, signal)


@pytest.mark.asyncio
async def test_waiting_reader_still_refuses_a_crashed_staging_manifest_without_hydration(tmp_path, monkeypatch):
    _, service, source, intent, _, _ = await _unrecorded(tmp_path)
    waiting, _ = _controlled_sections(service)
    staging, release = asyncio.Event(), asyncio.Event()

    async def crash(*args, **kwargs):
        staging.set()
        await release.wait()
        raise RuntimeError("controlled recorder crash")

    async def no_hydration(*args, **kwargs):
        raise AssertionError("a staging manifest must never hydrate its candidate")

    monkeypatch.setattr(intent_links, "write_hidden_version", crash)
    monkeypatch.setattr(intent_links, "load_version", no_hydration)
    writer = asyncio.create_task(source.record(intent), name="overlap-recorder")
    reader = signal = None
    try:
        await asyncio.wait_for(staging.wait(), 3)
        before = await read_json_or_none(source._path(TXID))
        reader = asyncio.create_task(source.load(TXID), name="overlap-reader")
        signal = asyncio.create_task(waiting.wait())
        done, _ = await asyncio.wait((reader, signal), timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert signal in done and not reader.done(), "reader did not wait for the recorder"
        release.set()
        with pytest.raises(RuntimeError, match="controlled recorder crash"):
            await writer
        with pytest.raises(DecisionRefused, match="card_intent_incomplete"):
            await reader
        assert await read_json_or_none(source._path(TXID)) == before
    finally:
        release.set()
        await _cancel_tasks(writer, reader, signal)


@pytest.mark.asyncio
async def test_reader_rechecks_immutable_binding_after_wait_and_hydrates_nothing_changed(tmp_path, monkeypatch):
    _, service, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    waiting, _ = _controlled_sections(service)
    raw = await read_json_or_none(source._path(TXID))
    reader = None
    reader_context = contextvars.copy_context()  # Capture before the writer acquires its R operation context.

    async def no_hydration(*args, **kwargs):
        raise AssertionError("changed manifest must refuse before Card hydration")

    monkeypatch.setattr(intent_links, "load_version", no_hydration)
    try:
        async with service._card_version_sections([(raw["subject_hash"], raw["access_id"])]):
            reader = asyncio.create_task(source.load(TXID), name="overlap-reader", context=reader_context)
            await asyncio.wait_for(waiting.wait(), 3)
            changed = {**raw, "record_digest": "d" * 64}
            await write_json_atomic(source._path(TXID), changed)
        with pytest.raises(DecisionRefused, match="card_intent_conflict"):
            await reader
        assert await read_json_or_none(source._path(TXID)) == changed
    finally:
        await _cancel_tasks(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True], ids=["single", "group"])
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_overlapping_prepare_observes_exact_retired_decision_without_hydration(
        tmp_path, monkeypatch, group, decision):
    setup = _unrecorded_group if group else _unrecorded
    store, _, source, intent, hub, decisions = await setup(tmp_path)
    await source.record(intent)
    waiting, release = asyncio.Event(), asyncio.Event()
    actual_sections = intent_links.sections

    @asynccontextmanager
    async def pause_reader(source, keys):
        if asyncio.current_task().get_name() == "retirement-reader":
            waiting.set()  # The first manifest read and nonterminal decision read have happened.
            await release.wait()
        async with actual_sections(source, keys):
            yield

    monkeypatch.setattr(intent_links, "sections", pause_reader)
    reader = asyncio.create_task(hub.prepare(TXID), name="retirement-reader")
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        await hub.prepare(TXID)
        await decisions.decide(TXID, decision)
        finished = await hub.finish(TXID, decision)
        assert not source._path(TXID).exists()
        receipt_path = transaction_store.receipt_path(store, TXID)
        receipt_bytes = receipt_path.read_bytes()

        async def no_hydration(*args, **kwargs):
            raise AssertionError("finished overlap must not hydrate or restage Card bodies")

        monkeypatch.setattr(intent_links, "load_version", no_hydration)
        release.set()
        reason = "card_transaction_late_stage" if decision == "committed" else "card_transaction_aborted"
        with pytest.raises(DecisionRefused, match=reason):
            await asyncio.wait_for(reader, 3)
        assert receipt_path.read_bytes() == receipt_bytes
        assert await hub.finish(TXID, decision) == finished
        with pytest.raises(DecisionRefused, match=reason):
            await hub.prepare(TXID)  # Fresh late PREPARE is still refused, not a new successful STAGE.
    finally:
        release.set()
        await _cancel_tasks(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True], ids=["single", "group"])
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_overlapping_finish_replays_exact_finished_receipt_without_hydration(
        tmp_path, monkeypatch, group, decision):
    setup = _unrecorded_group if group else _unrecorded
    store, _, source, intent, hub, decisions = await setup(tmp_path)
    await source.record(intent)
    await hub.prepare(TXID)
    await decisions.decide(TXID, decision)
    waiting, release = asyncio.Event(), asyncio.Event()
    actual_sections = intent_links.sections

    @asynccontextmanager
    async def pause_reader(source, keys):
        if asyncio.current_task().get_name() == "retirement-finisher":
            waiting.set()  # FINISH saw ready metadata and unfinished duties before its load waits.
            await release.wait()
        async with actual_sections(source, keys):
            yield

    monkeypatch.setattr(intent_links, "sections", pause_reader)
    reader = asyncio.create_task(hub.finish(TXID, decision), name="retirement-finisher")
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        finished = await hub.finish(TXID, decision)
        assert not source._path(TXID).exists()
        receipt_path = transaction_store.receipt_path(store, TXID)
        receipt_bytes = receipt_path.read_bytes()

        async def no_hydration(*args, **kwargs):
            raise AssertionError("duplicate FINISH must replay, not hydrate retired Card bodies")

        monkeypatch.setattr(intent_links, "load_version", no_hydration)
        release.set()
        assert await asyncio.wait_for(reader, 3) == finished
        assert receipt_path.read_bytes() == receipt_bytes
        assert await hub.finish(TXID, decision) == finished
    finally:
        release.set()
        await _cancel_tasks(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["record_digest", "authority", "scope"])
async def test_retired_overlap_refuses_changed_finished_binding(tmp_path, field):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    await source.record(intent)
    raw = await read_json_or_none(source._path(TXID))
    await hub.prepare(TXID)
    await decisions.decide(TXID, "committed")
    await hub.finish(TXID, "committed")
    receipt = await transaction_store.read_receipt(store, TXID)
    receipt["intent_binding"][field] = "d" * 64 if field == "record_digest" else "another-binding"
    await write_json_atomic(transaction_store.receipt_path(store, TXID), receipt)
    with pytest.raises(DecisionRefused, match="card_intent_conflict"):
        await intent_links.load_recorded_links(source, raw, TXID)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_initially_retired_load_uses_only_finished_terminal_metadata(tmp_path, monkeypatch, decision):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    await source.record(intent)
    await hub.prepare(TXID)
    await decisions.decide(TXID, decision)
    await hub.finish(TXID, decision)

    async def no_hydration(*args, **kwargs):
        raise AssertionError("retired load must not hydrate Card bodies")

    monkeypatch.setattr(intent_links, "load_version", no_hydration)
    reason = "card_transaction_late_stage" if decision == "committed" else "card_transaction_aborted"
    with pytest.raises(DecisionRefused, match=reason):
        await source.load(TXID)
    receipt = await transaction_store.read_receipt(store, TXID)
    receipt.pop("intent_binding")
    receipt.pop("intent_finished")
    await write_json_atomic(transaction_store.receipt_path(store, TXID), receipt)
    with pytest.raises(DecisionRefused, match="card_intent_unknown"):
        await source.load(TXID)  # A terminal receipt without completed service duties proves no retirement.


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "binding"])
async def test_initial_absence_rechecks_a_manifest_created_before_presence_check(tmp_path, monkeypatch, operation):
    _, _, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    actual_read, first = read_json_or_none, True

    async def stale_absence(path):
        nonlocal first
        if path == source._path(TXID) and first:
            first = False
            return None  # It was absent at read time, but the recorder publishes it before stat.
        return await actual_read(path)

    monkeypatch.setattr(card_participant, "read_json_or_none", stale_absence)
    monkeypatch.setattr(intent_links, "read_json_or_none", stale_absence)
    result = await getattr(source, operation)(TXID)
    if operation == "load":
        assert result.transaction_id == TXID
    else:
        assert result.scope == intent.scope


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "binding"])
async def test_manifest_json_null_remains_corruption_after_bounded_recheck(tmp_path, operation):
    _, _, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    await write_json_atomic(source._path(TXID), None)
    with pytest.raises(DecisionRefused, match="card_intent_invalid"):
        await getattr(source, operation)(TXID)

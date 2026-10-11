"""Controlled portable recorder/PREPARE ordering; cache and exclusion are declared fixture seams.

The real participant, manifest and version files run unchanged. This is not
SDK Redis/PG or live serving proof; the composed PB gate supplies those inputs.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from connection_hub.delegated_credentials.cards import intent_links
from connection_hub.delegated_credentials.durable_io import read_json_or_none, write_json_atomic
from test_w697_intent_links import TXID, _unrecorded, _unrecorded_group


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
        if signal is not None:
            signal.cancel()
        await asyncio.gather(*(task for task in (writer, reader, signal) if task is not None), return_exceptions=True)


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
        if signal is not None:
            signal.cancel()
        await asyncio.gather(*(task for task in (writer, reader, signal) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_reader_rechecks_immutable_binding_after_wait_and_hydrates_nothing_changed(tmp_path, monkeypatch):
    _, service, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    waiting, _ = _controlled_sections(service)
    raw = await read_json_or_none(source._path(TXID))
    reader = None

    async def no_hydration(*args, **kwargs):
        raise AssertionError("changed manifest must refuse before Card hydration")

    monkeypatch.setattr(intent_links, "load_version", no_hydration)
    try:
        async with service._card_version_sections([(raw["subject_hash"], raw["access_id"])]):
            reader = asyncio.create_task(source.load(TXID), name="overlap-reader")
            await asyncio.wait_for(waiting.wait(), 3)
            changed = {**raw, "record_digest": "d" * 64}
            await write_json_atomic(source._path(TXID), changed)
        with pytest.raises(DecisionRefused, match="card_intent_conflict"):
            await reader
        assert await read_json_or_none(source._path(TXID)) == changed
    finally:
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)

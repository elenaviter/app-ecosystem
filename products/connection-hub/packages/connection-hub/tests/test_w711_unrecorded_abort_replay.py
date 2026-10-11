"""Real signed admission/authority/answers for an ABORT with no local intent."""

import asyncio
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import LocalCardIntentSource, PARTICIPANT
from connection_hub.delegated_credentials.cards.participant_operation import CardTransactionParticipantOperation
from connection_hub.delegated_credentials.durable_io import read_json_or_none, unlink_guarded_async, write_json_atomic
from test_card_participant_operation import NOW, PEER, PROJECT, TX, _record, _request, _verified, _world


async def _finish(world, operation=None, **request_options):
    request = _request("finish", decision="aborted", **request_options)
    return _verified(await (operation or world.operation()).answer(request), request)


@pytest.mark.asyncio
async def test_unrecorded_abort_has_durable_binding_and_identical_authenticated_replays(tmp_path):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    first = await _finish(world)
    assert first["kind"] == "receipt"
    # Reconstruct the operation/participant: no remembered caller or cached answer.
    assert await _finish(world) == first
    assert await _finish(world, world.operation(enabled=False)) == first
    source = LocalCardIntentSource(world.store)
    binding = await source.binding(TX)
    assert (binding.authority, binding.scope) == (PEER, PROJECT)
    terminal = await read_json_or_none(tx.tombstone_path(world.store, TX))
    assert terminal["intent_digest"] == world.record.intent.digest
    assert terminal["intent_unrecorded"] is True and terminal["intent_finished"] is True
    assert terminal["intent_binding"]["record_digest"] != terminal["intent_digest"]
    assert not source._path(TX).exists()
    assert await tx.read_receipt(world.store, TX) is None
    assert not tx.active_path(world.store, TX).exists()
    request = _request("prepare")
    assert _verified(await world.operation().answer(request), request)["kind"] == "refused"
    assert await read_json_or_none(tx.tombstone_path(world.store, TX)) == terminal


@pytest.mark.asyncio
async def test_unrecorded_abort_cannot_transfer_to_another_authenticated_authority(tmp_path):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    assert (await _finish(world))["kind"] == "receipt"
    other = replace(world.caller, service_id="other-authority")
    operation = CardTransactionParticipantOperation(callers={other.service_id: other}, card_store=world.store,
                                                    nonces=world.nonces, enabled=True, clock=lambda: NOW)
    result = await _finish(world, operation, service_id=other.service_id)
    assert result == {"kind": "refused", "code": "card_intent_not_bound", "status": 409}


@pytest.mark.asyncio
async def test_unrecorded_abort_cannot_transfer_to_another_authenticated_scope_or_intent(tmp_path):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    assert (await _finish(world))["kind"] == "receipt"
    other_scope = "work:project:other"
    payload = world.record.intent.as_mapping()["payload"]
    world.records[(other_scope, TX)] = _record(other_scope, TX, payload["participant_inputs"][PARTICIPANT],
                                             payload["participant_candidates"][PARTICIPANT])
    assert (await _finish(world, scope=other_scope))["code"] == "card_intent_not_bound"
    world.records[(PROJECT, TX)] = _record(PROJECT, TX, payload["participant_inputs"][PARTICIPANT],
                                           payload["participant_candidates"][PARTICIPANT], request_id="different")
    assert (await _finish(world))["code"] == "card_intent_not_bound"


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["digest", "binding_digest", "unfinished", "origin", "unknown", "null"])
async def test_corrupt_terminal_is_signed_refusal_without_repair(tmp_path, corruption):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    assert (await _finish(world))["kind"] == "receipt"
    path = tx.tombstone_path(world.store, TX)
    raw = await read_json_or_none(path)
    if corruption == "digest":
        raw["intent_digest"] = "d" * 64
    elif corruption == "binding_digest":
        raw["intent_binding"]["record_digest"] = "e" * 64
    elif corruption == "unfinished":
        raw["intent_finished"] = False
    elif corruption == "origin":
        raw["intent_unrecorded"] = False
    elif corruption == "unknown":
        raw["untrusted"] = True
    else:
        raw = None
    await write_json_atomic(path, raw)
    result = await _finish(world)
    assert result["kind"] == "refused"
    assert result["code"] in {"card_intent_invalid", "card_intent_not_bound"}
    assert await read_json_or_none(path) == raw


@pytest.mark.asyncio
async def test_legacy_unbound_tombstone_is_not_adopted_from_a_new_caller(tmp_path):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    path = tx.tombstone_path(world.store, TX)
    legacy = {"transaction_id": TX, "state": "aborted"}
    await write_json_atomic(path, legacy)
    assert (await _finish(world))["code"] == "card_intent_not_bound"
    assert await read_json_or_none(path) == legacy


@pytest.mark.asyncio
async def test_parallel_never_recorded_finishes_return_the_same_receipt(tmp_path):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    results = await asyncio.gather(*(_finish(world) for _ in range(8)))
    assert all(result["kind"] == "receipt" for result in results)
    assert all(result == results[0] for result in results)


@pytest.mark.asyncio
async def test_interrupted_cleanup_retries_exact_binding_without_rewriting_terminal(tmp_path, monkeypatch):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    await write_json_atomic(tx.active_path(world.store, TX), {"transaction_id": TX, "collection_id": ""})
    original = tx._release_catalog

    async def interrupted(*args, **kwargs):
        raise OSError("synthetic cleanup interruption")

    monkeypatch.setattr(tx, "_release_catalog", interrupted)
    assert (await _finish(world))["kind"] == "refused"
    terminal = await read_json_or_none(tx.tombstone_path(world.store, TX))
    assert terminal["intent_unrecorded"] is True
    assert tx.active_path(world.store, TX).exists()
    monkeypatch.setattr(tx, "_release_catalog", original)
    first = await _finish(world)
    assert first["kind"] == "receipt" and await _finish(world) == first
    assert await read_json_or_none(tx.tombstone_path(world.store, TX)) == terminal
    assert not tx.active_path(world.store, TX).exists()


@pytest.mark.asyncio
async def test_prepared_receipt_without_a_manifest_is_not_absence(tmp_path):
    world, _, _ = await _world(tmp_path)
    request = _request("prepare")
    assert _verified(await world.operation().answer(request), request)["kind"] == "receipt"
    receipt = await tx.read_receipt(world.store, TX)
    await unlink_guarded_async(LocalCardIntentSource(world.store)._path(TX))
    world.decision = "aborted"
    assert (await _finish(world))["code"] == "card_intent_not_bound"
    assert await tx.read_receipt(world.store, TX) == receipt
    assert not tx.tombstone_path(world.store, TX).exists()


@pytest.mark.asyncio
async def test_failed_atomic_tombstone_write_leaves_no_orphan_binding(tmp_path, monkeypatch):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    original = tx.write_json_atomic

    async def interrupted(*args, **kwargs):
        raise OSError("synthetic publication interruption")

    monkeypatch.setattr(tx, "write_json_atomic", interrupted)
    assert (await _finish(world))["kind"] == "refused"
    assert await LocalCardIntentSource(world.store).binding(TX) is None
    assert not tx.tombstone_path(world.store, TX).exists()
    monkeypatch.setattr(tx, "write_json_atomic", original)
    first = await _finish(world)
    assert first["kind"] == "receipt" and await _finish(world) == first


@pytest.mark.asyncio
async def test_direct_participant_replay_retries_interrupted_absence_cleanup(tmp_path, monkeypatch):
    world, _, _ = await _world(tmp_path)
    world.decision = "aborted"
    original = tx._release_catalog

    async def interrupted(*args, **kwargs):
        raise OSError("synthetic cleanup interruption")

    monkeypatch.setattr(tx, "_release_catalog", interrupted)
    assert (await _finish(world))["kind"] == "refused"
    with pytest.raises(OSError, match="synthetic cleanup interruption"):
        await world.bind(PROJECT).participant.finish(TX, "aborted")
    monkeypatch.setattr(tx, "_release_catalog", original)
    first = await _finish(world)
    from dataclasses import asdict
    assert asdict(await world.bind(PROJECT).participant.finish(TX, "aborted")) == first["receipt"]

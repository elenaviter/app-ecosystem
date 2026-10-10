"""One durable Card body, links-only intents, exact crash/retry and FINISH boundaries."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from connection_hub.delegated_credentials.cards import intent_links, transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    CardIntent, DecisionStorePort, HubCardParticipant, LocalCardIntentSource, PARTICIPANT,
)
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.version_link import LINK_KEYS, staging_tag, write_hidden_version
from connection_hub.delegated_credentials.durable_io import read_json_or_none, write_json_atomic
from test_card_participant import TXID, WITNESS, _Store, _draft, _edit
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import _setup


async def _unrecorded(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    decisions = _Store()
    row = await decisions.begin(_draft(before, after, effects=()))
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    source = LocalCardIntentSource(store, service=service, decisions=decisions)
    intent = CardIntent(TXID, row.intent.digest, SUBJECT_HASH, before, after,
                        action="update", actor_subject="person", actor_kind="caller")
    hub = HubCardParticipant(service=service, store=store, intents=source, decisions=decisions)
    return store, service, source, intent, hub, decisions


@pytest.mark.asyncio
async def test_single_record_is_links_only_and_stage_never_writes_a_second_version(tmp_path):
    store, coordinator, _, draft, before, _, _, _ = await _edit(tmp_path, effects=())
    source = LocalCardIntentSource(store)
    raw = await read_json_or_none(source._path(TXID))
    assert raw["schema"] == intent_links.SINGLE_SCHEMA and raw["status"] == "ready"
    assert set(raw["original"]) == set(raw["candidate"]) == LINK_KEYS
    versions = set(store.card_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).glob("revisions/*.json"))
    await coordinator.prepare(draft)
    assert set(store.card_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).glob("revisions/*.json")) == versions
    assert (await tx.read_receipt(store, TXID))["after"]["revision_name"] == raw["candidate"]["revision_name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_finish_removes_intent_and_replays_without_card_hydration(tmp_path, monkeypatch, decision):
    store, coordinator, _, draft, _, _, _, _ = await _edit(tmp_path, effects=())
    await coordinator.prepare(draft)
    await coordinator.decide(TXID, decision, **({"witness_digest": WITNESS} if decision == "committed" else {}))
    hub = coordinator.participants[PARTICIPANT]
    first = await hub.finish(TXID, decision)
    source = LocalCardIntentSource(store)
    assert not source._path(TXID).exists()

    async def no_body(*args, **kwargs):
        raise AssertionError("terminal retry must not hydrate Card bodies")

    monkeypatch.setattr(intent_links, "load_version", no_body)
    assert await hub.finish(TXID, decision) == first
    binding = await source.binding(TXID)
    assert binding.authority == binding.scope == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [1, 2])
@pytest.mark.parametrize("recovery", ["resume", "abort"])
async def test_manifest_crash_is_exactly_resumable_or_abortable(tmp_path, monkeypatch, boundary, recovery):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    real_write, count = intent_links.write_json_atomic, 0

    async def crash(path, payload):
        nonlocal count
        count += 1
        if count == boundary:
            raise RuntimeError("intent crash")
        await real_write(path, payload)

    monkeypatch.setattr(intent_links, "write_json_atomic", crash)
    with pytest.raises(RuntimeError, match="intent crash"):
        await source.record(intent)
    monkeypatch.setattr(intent_links, "write_json_atomic", real_write)
    raw = await read_json_or_none(source._path(TXID))
    if raw is not None:
        assert raw["status"] == "staging"
        with pytest.raises(DecisionRefused, match="card_intent_incomplete"):
            await source.load(TXID)
    if recovery == "resume":
        await source.record(intent)
        assert (await source.load(TXID)).candidate == intent.candidate
        if raw is not None:
            assert (await read_json_or_none(source._path(TXID)))["candidate"] == raw["candidate"]
    else:
        await decisions.decide(TXID, "aborted")
        first = await hub.finish(TXID, "aborted")
        assert not source._path(TXID).exists()
        assert await hub.finish(TXID, "aborted") == first
        if raw is not None:
            assert not store.revision_path(subject_hash=SUBJECT_HASH, access_id=intent.candidate.access_id,
                                           revision_name=raw["candidate"]["revision_name"]).exists()


@pytest.mark.asyncio
async def test_hidden_version_write_crash_retains_manifest_for_abort(tmp_path, monkeypatch):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)

    async def crash(*args, **kwargs):
        raise RuntimeError("version crash")

    monkeypatch.setattr(intent_links, "write_hidden_version", crash)
    with pytest.raises(RuntimeError, match="version crash"):
        await source.record(intent)
    raw = await read_json_or_none(source._path(TXID))
    assert raw["status"] == "staging" and set(raw["candidate"]) == LINK_KEYS
    await decisions.decide(TXID, "aborted")
    await hub.finish(TXID, "aborted")
    assert not source._path(TXID).exists()


@pytest.mark.asyncio
async def test_plan_candidate_is_adopted_without_a_new_file(tmp_path):
    store, service, source, intent, hub, _ = await _unrecorded(tmp_path)
    at = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
    tag = staging_tag("oauth-issuance-candidate", "one-plan")
    async with service._card_version_sections([(SUBJECT_HASH, intent.candidate.access_id)]):
        link = await write_hidden_version(store, subject_hash=SUBJECT_HASH, authority=intent.candidate, at=at, tag=tag)
    path = store.revision_path(subject_hash=SUBJECT_HASH, access_id=intent.candidate.access_id,
                               revision_name=link["revision_name"])
    original_bytes = path.read_bytes()
    await source.record(intent, candidate_link=link, candidate_staging_tag=tag)
    await hub.prepare(TXID)
    assert path.read_bytes() == original_bytes
    raw = await read_json_or_none(source._path(TXID))
    assert raw["candidate"] == link
    assert await read_json_or_none(tx.revision_marker_path(store, subject_hash=SUBJECT_HASH,
        access_id=intent.candidate.access_id, revision_name=link["revision_name"])) == {"transaction_id": TXID}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["label", "hash", "revision", "access_id"])
async def test_exact_link_checks_refuse_substituted_or_changed_versions(tmp_path, changed):
    store, _, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    raw = await read_json_or_none(source._path(TXID))
    path = store.revision_path(subject_hash=SUBJECT_HASH, access_id=intent.candidate.access_id,
                               revision_name=raw["candidate"]["revision_name"])
    if changed == "hash":
        raw["candidate"]["content_hash"] = "0" * 64
        await write_json_atomic(source._path(TXID), raw)
    else:
        payload = await read_json_or_none(path)
        field = {"revision": "card_revision"}.get(changed, changed)
        payload[field] = 99 if changed == "revision" else "substituted"
        await write_json_atomic(path, payload)
    with pytest.raises(DecisionRefused):
        await source.load(TXID)


@pytest.mark.asyncio
async def test_json_null_intent_is_corruption_not_absence(tmp_path):
    _, _, source, intent, _, _ = await _unrecorded(tmp_path)
    source._path(TXID).parent.mkdir(parents=True, exist_ok=True)
    source._path(TXID).write_text("null")
    with pytest.raises(DecisionRefused, match="card_intent_invalid"):
        await source.record(intent)
    assert source._path(TXID).read_text() == "null"


@pytest.mark.asyncio
async def test_bound_candidate_mismatch_refuses_before_manifest_write(tmp_path):
    _, _, source, intent, _, _ = await _unrecorded(tmp_path)
    with pytest.raises(DecisionRefused, match="card_intent_not_bound"):
        await source.record(replace(intent, candidate=replace(intent.candidate, label="swapped")))
    assert not source._path(TXID).exists()


@pytest.mark.asyncio
async def test_post_decision_service_failure_retains_intent_until_finish_succeeds(tmp_path, monkeypatch):
    store, coordinator, _, draft, _, _, _, _ = await _edit(tmp_path, effects=())
    await coordinator.prepare(draft)
    await coordinator.decide(TXID, "committed", witness_digest=WITNESS)
    hub = coordinator.participants[PARTICIPANT]
    real = hub._service._cache.commit_projection

    async def fail(*args, **kwargs):
        raise RuntimeError("projection offline")

    monkeypatch.setattr(hub._service._cache, "commit_projection", fail)
    with pytest.raises(CardServingUnavailable):
        await hub.finish(TXID, "committed")
    assert LocalCardIntentSource(store)._path(TXID).exists()
    monkeypatch.setattr(hub._service._cache, "commit_projection", real)
    await hub.finish(TXID, "committed")
    assert not LocalCardIntentSource(store)._path(TXID).exists()


@pytest.mark.asyncio
async def test_exact_terminal_record_replay_writes_nothing_but_changed_replay_refuses(tmp_path):
    _, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    await source.record(intent)
    await hub.prepare(TXID)
    await decisions.decide(TXID, "committed")
    await hub.finish(TXID, "committed")
    await source.record(intent)
    assert not source._path(TXID).exists()
    with pytest.raises(DecisionRefused):
        await source.record(replace(intent, candidate=replace(intent.candidate, label="late swap")))
    with pytest.raises(DecisionRefused, match="card_transaction_late_stage"):
        await hub.prepare(TXID)


@pytest.mark.asyncio
async def test_late_first_record_refuses_after_global_abort_without_resurrecting_candidate(tmp_path):
    _, _, source, intent, _, decisions = await _unrecorded(tmp_path)
    await decisions.decide(TXID, "aborted")
    with pytest.raises(DecisionRefused, match="card_transaction_late_stage"):
        await source.record(intent)
    assert not source._path(TXID).exists()


@pytest.mark.asyncio
async def test_v2_record_rejects_a_hidden_full_body_field(tmp_path):
    _, _, source, intent, _, _ = await _unrecorded(tmp_path)
    await source.record(intent)
    raw = await read_json_or_none(source._path(TXID))
    raw["extra_body"] = intent.candidate.to_dict()
    await write_json_atomic(source._path(TXID), raw)
    with pytest.raises(DecisionRefused, match="card_intent_invalid"):
        await source.load(TXID)

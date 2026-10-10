"""One durable Card body, links-only intents, exact crash/retry and FINISH boundaries."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from connection_hub.delegated_credentials.cards import intent_links, transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    CardGroupIntent, CardGroupMemberIntent, CardIntent, DecisionStorePort, HubCardParticipant,
    LocalCardIntentSource, PARTICIPANT,
)
from connection_hub.delegated_credentials.cards.card_group import group_member, hub_group_participant_input
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.version_link import LINK_KEYS, staging_tag, write_hidden_version
from connection_hub.delegated_credentials.durable_io import read_json_or_none, write_json_atomic
from test_card_participant import TXID, WITNESS, _Store, _draft, _edit
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import _setup
from service_foundation.coordination.durable_decision_log import IntentDraft


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


async def _unrecorded_group(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    created = replace(after, access_id="aut_zz_new", card_revision=1)
    members = (CardGroupMemberIntent(SUBJECT_HASH, before, after, "update"),
               CardGroupMemberIntent(SUBJECT_HASH, None, created, "create"))
    projection = hub_group_participant_input(
        members=[group_member(original=m.original, candidate=m.candidate, action=m.action) for m in members],
        actor_subject="person", actor_kind="caller")
    decisions = _Store()
    row = await decisions.begin(IntentDraft(
        replay_scope="test:group", request_id="group", expires_at=2_000_000_000,
        participants=(PARTICIPANT,), payload={"participant_inputs": {PARTICIPANT: projection}}))
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    source = LocalCardIntentSource(store, service=service, decisions=decisions)
    intent = CardGroupIntent(TXID, row.intent.digest, members, actor_subject="person", actor_kind="caller")
    hub = HubCardParticipant(service=service, store=store, intents=source, decisions=decisions)
    return store, service, source, intent, hub, decisions


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["resume", "abort"])
async def test_partial_group_manifest_names_every_member_before_any_version(tmp_path, monkeypatch, recovery):
    store, _, source, intent, hub, decisions = await _unrecorded_group(tmp_path)
    write, count = intent_links.write_hidden_version, 0

    async def crash_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("second member crash")
        return await write(*args, **kwargs)

    monkeypatch.setattr(intent_links, "write_hidden_version", crash_second)
    with pytest.raises(RuntimeError, match="second member crash"):
        await source.record(intent)
    raw = await read_json_or_none(source._path(TXID))
    assert raw["status"] == "staging" and len(raw["members"]) == 2
    with pytest.raises(DecisionRefused, match="card_intent_incomplete"):
        await source.load(TXID)
    monkeypatch.setattr(intent_links, "write_hidden_version", write)
    if recovery == "resume":
        await source.record(intent)
        assert (await read_json_or_none(source._path(TXID)))["members"] == raw["members"]
        await hub.prepare(TXID)
        await decisions.decide(TXID, "committed")
        await hub.finish(TXID, "committed")
    else:
        await decisions.decide(TXID, "aborted")
        await hub.finish(TXID, "aborted")
        for member in raw["members"]:
            assert not store.revision_path(subject_hash=member["subject_hash"], access_id=member["access_id"],
                                           revision_name=member["candidate"]["revision_name"]).exists()
    assert not source._path(TXID).exists()


@pytest.mark.asyncio
async def test_group_plan_candidate_is_adopted_by_its_member_id_not_parent_id(tmp_path):
    store, service, source, intent, hub, decisions = await _unrecorded_group(tmp_path)
    lead = intent.members[0]
    tag = staging_tag("oauth-issuance-candidate", "group-plan")
    async with service._card_version_sections([(lead.subject_hash, lead.candidate.access_id)]):
        link = await write_hidden_version(store, subject_hash=lead.subject_hash, authority=lead.candidate,
                                          at=datetime.now(timezone.utc), tag=tag)
    planned = replace(lead, candidate_link=link, candidate_staging_tag=tag)
    await source.record(replace(intent, members=(planned, intent.members[1])))
    path = store.revision_path(subject_hash=lead.subject_hash, access_id=lead.candidate.access_id,
                               revision_name=link["revision_name"])
    original_bytes = path.read_bytes()
    assert await read_json_or_none(tx.revision_marker_path(
        store, subject_hash=lead.subject_hash, access_id=lead.candidate.access_id,
        revision_name=link["revision_name"])) == {"transaction_id": tx.member_transaction_id(TXID, 0)}
    await hub.prepare(TXID)
    assert path.read_bytes() == original_bytes
    assert (await tx.read_receipt(store, tx.member_transaction_id(TXID, 0)))["after"]["revision_name"] == link["revision_name"]
    await decisions.decide(TXID, "committed")
    await hub.finish(TXID, "committed")


@pytest.mark.asyncio
async def test_cleanup_crash_retains_address_and_retry_never_hydrates_deleted_candidate(tmp_path, monkeypatch):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    await source.record(intent)
    await decisions.decide(TXID, "aborted")
    unlink = intent_links.unlink_guarded
    raw = await read_json_or_none(source._path(TXID))
    marker_path = tx.revision_marker_path(store, subject_hash=SUBJECT_HASH,
        access_id=intent.candidate.access_id, revision_name=raw["candidate"]["revision_name"])

    def crash_marker(path):
        if path == marker_path:
            raise OSError("cleanup interrupted")
        unlink(path)

    monkeypatch.setattr(intent_links, "unlink_guarded", crash_marker)
    with pytest.raises(OSError, match="cleanup interrupted"):
        await hub.finish(TXID, "aborted")
    assert source._path(TXID).exists() and marker_path.exists()
    assert (await read_json_or_none(tx.tombstone_path(store, TXID)))["intent_finished"] is True
    monkeypatch.setattr(intent_links, "unlink_guarded", unlink)

    async def no_body(*args, **kwargs):
        raise AssertionError("cleanup retry must not hydrate the deleted body")

    monkeypatch.setattr(intent_links, "load_version", no_body)
    first = await hub.finish(TXID, "aborted")
    assert not source._path(TXID).exists() and not marker_path.exists()
    assert await hub.finish(TXID, "aborted") == first


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_unfinished_legacy_v1_intent_can_finish_and_be_retired(tmp_path, decision):
    store, _, source, intent, hub, decisions = await _unrecorded(tmp_path)
    await write_json_atomic(source._path(TXID), intent.to_dict())
    await hub.prepare(TXID)
    await decisions.decide(TXID, decision)
    first = await hub.finish(TXID, decision)
    assert not source._path(TXID).exists()
    assert await hub.finish(TXID, decision) == first


@pytest.mark.asyncio
async def test_concurrent_exact_record_is_serialized_and_preserves_one_frozen_name(tmp_path):
    store, service, source, intent, _, _ = await _unrecorded(tmp_path)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def card_lock(**kwargs):
        async with lock:
            yield

    service._mutation_lock = card_lock
    await asyncio.gather(source.record(intent), source.record(intent), source.record(intent))
    raw = await read_json_or_none(source._path(TXID))
    assert len(list(store.card_path(subject_hash=SUBJECT_HASH, access_id=intent.candidate.access_id)
                    .glob("revisions/*[0-9a-f].json"))) == 2  # bodies, not *.card-transaction.json markers
    assert (await source.load(TXID)).candidate_link == raw["candidate"]


@pytest.mark.asyncio
async def test_external_first_prepare_uses_recorded_frozen_link_and_finished_cursor(tmp_path):
    from test_card_participant_operation import _world, _request, _verified, TX, PROJECT

    world, before, _ = await _world(tmp_path)
    operation = world.operation()
    request = _request("prepare")
    assert _verified(await operation.answer(request), request)["kind"] == "receipt"
    raw = await read_json_or_none(LocalCardIntentSource(world.store)._path(TX))
    assert (await tx.read_receipt(world.store, TX))["after"]["revision_name"] == raw["candidate"]["revision_name"]
    assert len(list(world.store.card_path(subject_hash=raw["subject_hash"], access_id=before.access_id)
                    .glob("revisions/*[0-9a-f].json"))) == 2
    world.decision = "committed"
    request = _request("finish", decision="committed")
    first = _verified(await operation.answer(request), request)
    assert first["kind"] == "receipt"
    request = _request("finish", decision="committed")
    assert _verified(await operation.answer(request), request) == first
    request = _request("list_prepared", transaction_id=None, limit=1, cursor=TX, scope=PROJECT)
    assert _verified(await operation.answer(request), request)["kind"] == "page"
    request = _request("list_prepared", transaction_id=None, limit=1, cursor=TX, scope="work:project:other")
    assert _verified(await operation.answer(request), request)["code"] == "card_participant_cursor_invalid"

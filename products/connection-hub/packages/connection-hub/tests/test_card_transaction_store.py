"""W578: the generic staged Card participant on the real filesystem store and Card service."""

from __future__ import annotations

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import CardConflict, CardServingUnavailable, DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
from test_card_service import _Cache, _authority, SUBJECT_HASH, NOW

TX = "a" * 64
INTENT = "b" * 64


class Decisions:
    """PB's recorded decision, per transaction: undecided until the coordinator records one."""

    def __init__(self):
        self.recorded = {}
        self.fail = False

    async def decision(self, receipt):
        if self.fail:
            raise RuntimeError("coordinator unreachable")
        return self.recorded.get(receipt["transaction_id"], "undecided")


def _record(store, decision, transaction_id=TX):
    store._card_transaction_decisions.recorded[transaction_id] = decision


async def _decide(store, decision, transaction_id=TX, **kwargs):
    _record(store, decision, transaction_id)
    return await tx.decide(store, transaction_id=transaction_id, intent_digest=INTENT, decision=decision, **kwargs)


async def _setup(tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    before = _authority()
    await service.commit(before, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    after = replace(before, card_revision=before.card_revision + 1, label="staged change")
    return store, service, before, after


async def _visible(store, card):
    return (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id))[1]


async def _undecided(store, card):
    with pytest.raises(CardStorageError, match="card_transaction_undecided"):
        await _visible(store, card)


async def _stage(store, before, after, **changes):
    from datetime import datetime, timezone
    when = NOW if isinstance(NOW, datetime) else datetime.fromtimestamp(NOW, timezone.utc)
    values = dict(transaction_id=TX, intent_digest=INTENT, participant="project", subject_hash=SUBJECT_HASH,
                  original=before, candidate=after, now=when)
    values.update(changes)
    return await tx.stage(store, **values)


@pytest.mark.asyncio
async def test_a_staged_change_is_invisible_until_the_recorded_decision_commits_it(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    receipt = await _stage(store, before, after)
    assert receipt["state"] == "prepared"
    await _undecided(store, before)  # readers refuse while PB has recorded no decision
    decided = await _decide(store, "committed")
    assert decided["state"] == "committed"
    assert await _visible(store, before) == after  # the one rename made AFTER visible


@pytest.mark.asyncio
async def test_an_aborted_transaction_leaves_before_and_releases_the_card(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    await _decide(store, "aborted", reason="last_admin")
    assert await _visible(store, before) == before
    # An ordinary write proceeds again once the transaction is decided.
    nxt = replace(before, card_revision=before.card_revision + 1, label="ordinary edit")
    await service.commit(nxt, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    assert await _visible(store, before) == nxt


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["commit", "revoke"])
async def test_no_ordinary_writer_publishes_around_an_undecided_staged_card(tmp_path, writer):
    store, service, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    with pytest.raises(CardConflict, match="card_transaction_unresolved"):
        if writer == "commit":
            await service.commit(replace(before, card_revision=before.card_revision + 1, label="bypass"),
                                 subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
        else:
            await service.revoke(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                 expected_revision=before.card_revision)
    await _undecided(store, before)


@pytest.mark.asyncio
async def test_the_decision_is_exactly_once_and_bound_to_the_intent(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_intent_mismatch"):
        await tx.decide(store, transaction_id=TX, intent_digest="c" * 64, decision="committed")
    await _decide(store, "committed")
    again = await _decide(store, "committed")
    assert again["state"] == "committed"  # idempotent
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_conflict"):
        await _decide(store, "aborted")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_invalid"):
        await _decide(store, "maybe")


@pytest.mark.asyncio
async def test_staging_replays_exactly_and_refuses_a_changed_replay(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    first = await _stage(store, before, after)
    assert await _stage(store, before, after) == first
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await _stage(store, before, replace(after, label="different"))


@pytest.mark.asyncio
async def test_staging_refuses_a_moved_card_or_a_wrong_candidate(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_revision_moved"):
        await _stage(store, replace(before, label="not what is stored"), after)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_candidate_invalid"):
        await _stage(store, before, replace(after, card_revision=before.card_revision + 2))


@pytest.mark.asyncio
async def test_a_crash_after_the_prepared_receipt_leaves_the_card_readable_as_before(tmp_path, monkeypatch):
    store, _, before, after = await _setup(tmp_path)

    async def crash(**kwargs):
        raise RuntimeError("killed after the receipt, before the staged revision")

    monkeypatch.setattr(store, "write_revision", crash)
    with pytest.raises(RuntimeError):
        await _stage(store, before, after)
    assert await _visible(store, before) == before
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
async def test_an_unknown_transaction_or_a_bad_id_is_refused(tmp_path):
    store, _, _, _ = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_unknown"):
        await _decide(store, "committed")
    with pytest.raises(CardStorageError, match="card_transaction_id_invalid"):
        await tx.state(store, transaction_id="not-hex")


@pytest.mark.asyncio
async def test_the_service_stages_under_the_fence_and_serves_only_the_committed_decision(tmp_path):
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    served = []

    async def projection(authority, **kwargs):
        served.append(authority.card_revision)
        return True

    service._cache.commit_projection = projection
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    receipt = await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                              subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    assert receipt["state"] == "prepared"
    await _undecided(store, before)
    _record(store, "committed")
    decided = await service.decide_transaction(transaction_id=TX, intent_digest=INTENT, decision="committed",
                                               subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert decided["state"] == "committed" and await _visible(store, before) == after
    assert served == [after.card_revision]  # the committed AFTER is what gets served


@pytest.mark.asyncio
async def test_the_service_abort_serves_nothing_new(tmp_path):
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    served = []

    async def projection(authority, **kwargs):
        served.append(authority.card_revision)
        return True

    service._cache.commit_projection = projection
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    _record(store, "aborted")
    decided = await service.decide_transaction(transaction_id=TX, intent_digest=INTENT, decision="aborted",
                                               subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert decided["state"] == "aborted" and await _visible(store, before) == before
    assert served == []  # an abort serves nothing new


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_staged_revoke_takes_effect_only_on_commit_and_serves_its_tombstone(tmp_path, decision):
    from datetime import datetime, timezone
    from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
    from connection_hub.delegated_credentials.cards.service import replace_state
    store, service, before, _ = await _setup(tmp_path)
    tombstones = []

    async def tombstone(access_id, **kwargs):
        tombstones.append(kwargs["card_revision"])
        return True

    service._cache.commit_tombstone = tombstone
    revoked = replace_state(before, CARD_STATE_REVOKED)
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=revoked, now=when)
    await _undecided(store, before)  # never served as revoked (nor as current) while undecided
    _record(store, decision)
    await service.decide_transaction(transaction_id=TX, intent_digest=INTENT, decision=decision,
                                     subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    visible = await _visible(store, before)
    if decision == "committed":
        assert visible.state == CARD_STATE_REVOKED and tombstones == [revoked.card_revision]
    else:
        assert visible == before and tombstones == []


# ── Ops review F1 to F4 (11:13) ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_at", ["write_revision", "pointer"])
async def test_a_crash_mid_stage_is_fenced_and_a_replay_resumes_it(tmp_path, monkeypatch, crash_at):
    store, service, before, after = await _setup(tmp_path)
    if crash_at == "write_revision":
        original = store.write_revision

        async def crash(**kwargs):
            raise RuntimeError("killed")

        monkeypatch.setattr(store, "write_revision", crash)
    else:
        original_write = tx.write_json_atomic
        calls = {"n": 0}

        async def crash_on_pointer(path, payload):
            if payload.get("schema") == tx.TRANSACTION_POINTER_SCHEMA:
                raise RuntimeError("killed")
            return await original_write(path, payload)

        monkeypatch.setattr(tx, "write_json_atomic", crash_on_pointer)
    with pytest.raises(RuntimeError):
        await _stage(store, before, after)
    # F1: the Card is fenced before the pointer exists.
    with pytest.raises(CardConflict, match="card_transaction_unresolved"):
        await service.commit(replace(before, card_revision=before.card_revision + 1, label="ordinary writer"),
                             subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    # A commit decision before the pointer is in place is refused, not reported.
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_not_staged"):
        await _decide(store, "committed")
    monkeypatch.undo()
    # The replay resumes the missing steps; then the commit really publishes AFTER.
    await _stage(store, before, after)
    await _decide(store, "committed")
    assert await _visible(store, before) == after


@pytest.mark.asyncio
async def test_an_aborted_crash_mid_stage_releases_the_card(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)

    async def crash(**kwargs):
        raise RuntimeError("killed")

    monkeypatch.setattr(store, "write_revision", crash)
    with pytest.raises(RuntimeError):
        await _stage(store, before, after)
    monkeypatch.undo()
    await _decide(store, "aborted")
    nxt = replace(before, card_revision=before.card_revision + 1, label="ordinary edit")
    await service.commit(nxt, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    assert await _visible(store, before) == nxt


@pytest.mark.asyncio
async def test_stage_respects_an_unresolved_issuer_update_preparation(tmp_path, monkeypatch):
    # F2: a fresh stage passes the full shared fence.
    store, _, before, after = await _setup(tmp_path)
    from connection_hub.delegated_credentials.cards import lifecycle_store

    async def unresolved(store_, *, subject_hash, access_id):
        raise CardStorageError("issuer_update_preparation_unresolved")

    monkeypatch.setattr(lifecycle_store, "assert_pointer_replaceable", unresolved)
    with pytest.raises(CardStorageError, match="issuer_update_preparation_unresolved"):
        await _stage(store, before, after)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_a_second_transaction_cannot_stage_a_card_with_one_prepared(tmp_path):
    # F4: the concurrency fence, pinned.
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
        await _stage(store, before, replace(after, label="another"), transaction_id="d" * 64)


@pytest.mark.asyncio
async def test_the_serving_projection_is_marked_updating_before_the_commit_rename(tmp_path):
    # F3: no resolver serves BEFORE as current once the commit rename happens.
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    order = []

    async def mark(**kwargs):
        order.append(("mark", (await tx.state(store, transaction_id=TX))["state"]))

    async def projection(authority, **kwargs):
        order.append(("projection", authority.card_revision))
        return True

    service._mark_updating = mark
    service._cache.commit_projection = projection
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    _record(store, "committed")
    await service.decide_transaction(transaction_id=TX, intent_digest=INTENT, decision="committed",
                                     subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert order == [("mark", "prepared"), ("projection", after.card_revision)]


# ── One decision source: PB's recorded decision (CodeApp 11:14, Root 11:17) ──


@pytest.mark.asyncio
async def test_a_reader_follows_the_recorded_commit_before_local_materialization(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    _record(store, "committed")  # PB decided; the local receipt is still prepared
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"
    assert await _visible(store, before) == after


@pytest.mark.asyncio
async def test_a_reader_follows_the_recorded_abort_and_a_replay_cannot_restage_it(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    _record(store, "aborted")
    assert await _visible(store, before) == before
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_aborted"):
        await _stage(store, before, after)


@pytest.mark.asyncio
@pytest.mark.parametrize("port", ["unbound", "unreachable"])
async def test_without_a_reachable_decision_readers_and_decide_fail_closed(tmp_path, port):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    if port == "unbound":
        tx.bind_transaction_decisions(store, None)
    else:
        store._card_transaction_decisions.recorded[TX] = "committed"
        store._card_transaction_decisions.fail = True
    await _undecided(store, before)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_unverified"):
        await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="committed")
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded,attempted", [("undecided", "committed"), ("aborted", "committed"),
                                                ("committed", "aborted")])
async def test_local_decide_never_makes_an_independent_decision(tmp_path, recorded, attempted):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    _record(store, recorded)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_not_recorded"):
        await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision=attempted)
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


# ── Ops F7 (11:20): finishing COMMITTED always succeeds ─────────────────────


class _LuaCache:
    """The Redis marker rules, one key per Card: _INSTALL_MARKER_LUA and _FINALIZE_LUA in cards/cache.py."""

    def __init__(self):
        self.value = None  # {"kind", "card_revision", "mutation_id"}

    async def claim_transition(self, access_id, *, mutation_id, expected_revision, ttl_seconds):
        v = self.value
        if v is not None and (v["kind"] not in ("card", "revoked") or v["card_revision"] != expected_revision):
            return False
        self.value = {"kind": "updating", "card_revision": expected_revision, "mutation_id": mutation_id}
        return True

    def _finalize(self, payload, mutation_id, incoming):
        v = self.value
        if v is not None:
            owned = v["kind"] == "updating" and v.get("mutation_id") == mutation_id
            if not owned and (incoming <= 0 or v["kind"] != "card" or v["card_revision"] >= incoming):
                return False
        self.value = payload
        return True

    async def commit_projection(self, authority, *, mutation_id, ttl_seconds):
        return self._finalize({"kind": "card", "card_revision": authority.card_revision}, mutation_id,
                              authority.card_revision)

    async def commit_tombstone(self, access_id, *, card_revision, mutation_id, ttl_seconds):
        return self._finalize({"kind": "revoked", "card_revision": card_revision}, mutation_id, card_revision)

    async def finalize_removal(self, access_id, *, mutation_id):
        return self._finalize(None, mutation_id, 0)

    async def read(self, access_id):
        from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
        v = self.value
        return None if v is None else CardCacheEntry(kind=v["kind"], card_revision=v["card_revision"],
                                                     mutation_id=v.get("mutation_id", ""))

    async def reconcile_projection(self, *args, **kwargs):
        return False

    async def index_add(self, **kwargs):
        return None

    async def index_remove(self, **kwargs):
        return None


async def _served(tmp_path):
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    service._cache = cache = _LuaCache()
    cache.value = {"kind": "card", "card_revision": before.card_revision}
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    return store, service, cache, before, after


async def _service_decide(store, service, before, decision):
    _record(store, decision)
    return await service.decide_transaction(transaction_id=TX, intent_digest=INTENT, decision=decision,
                                            subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)


@pytest.mark.asyncio
async def test_replaying_a_committed_and_served_decision_succeeds(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)
    await _service_decide(store, service, before, "committed")
    assert cache.value == {"kind": "card", "card_revision": after.card_revision}
    again = await _service_decide(store, service, before, "committed")
    assert again["state"] == "committed"
    assert cache.value == {"kind": "card", "card_revision": after.card_revision}


@pytest.mark.asyncio
async def test_a_replay_finishes_serving_a_commit_a_crash_cut_short(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)

    async def crash(*args, **kwargs):
        raise RuntimeError("killed after the rename")

    real = cache.commit_projection
    cache.commit_projection = crash
    with pytest.raises(CardServingUnavailable):
        await _service_decide(store, service, before, "committed")
    assert cache.value["kind"] == "updating"  # readers stay closed meanwhile
    cache.commit_projection = real
    await _service_decide(store, service, before, "committed")
    assert cache.value == {"kind": "card", "card_revision": after.card_revision}


@pytest.mark.asyncio
async def test_a_retry_keeps_the_marker_its_own_earlier_attempt_left(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)
    from connection_hub.delegated_credentials.cards import transaction_store
    real = transaction_store.decide

    async def crash(*args, **kwargs):
        raise RuntimeError("killed between the mark and the rename")

    transaction_store.decide = crash
    try:
        with pytest.raises(RuntimeError):
            await _service_decide(store, service, before, "committed")
    finally:
        transaction_store.decide = real
    assert cache.value["kind"] == "updating"
    await _service_decide(store, service, before, "committed")
    assert cache.value == {"kind": "card", "card_revision": after.card_revision}


@pytest.mark.asyncio
async def test_an_abort_releases_the_marker_its_transaction_left(tmp_path):
    # Ops non-blocking (b): no 15 s fail-closed window after an aborted crash.
    store, service, cache, before, after = await _served(tmp_path)
    from connection_hub.delegated_credentials.cards.service import transaction_mutation_id
    cache.value = {"kind": "updating", "card_revision": before.card_revision,
                   "mutation_id": transaction_mutation_id(TX)}
    await _service_decide(store, service, before, "aborted")
    assert cache.value is None  # readers fall through to the durable BEFORE


@pytest.mark.asyncio
async def test_an_abort_never_removes_another_mutations_marker_or_projection(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)
    cache.value = {"kind": "updating", "card_revision": before.card_revision, "mutation_id": "someone-else"}
    await _service_decide(store, service, before, "aborted")
    assert cache.value["mutation_id"] == "someone-else"

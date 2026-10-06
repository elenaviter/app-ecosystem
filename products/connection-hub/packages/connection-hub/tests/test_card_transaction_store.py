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

    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    service._mark_updating = mark
    service._cache.commit_projection = projection
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

    redis = None  # the resolver's reconciler is never reached: every read is "in the current run"

    def __init__(self):
        self.value = None  # {"kind", "card_revision", "mutation_id"}

    def reconcile_lock_key(self):
        return "reconcile-lock"

    def projection_epoch_key(self):
        return "projection-epoch"

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
        return self._finalize({"kind": "card", "card_revision": authority.card_revision, "authority": authority},
                              mutation_id, authority.card_revision)

    async def commit_tombstone(self, access_id, *, card_revision, mutation_id, ttl_seconds):
        return self._finalize({"kind": "revoked", "card_revision": card_revision}, mutation_id, card_revision)

    async def finalize_removal(self, access_id, *, mutation_id):
        return self._finalize(None, mutation_id, 0)

    async def read(self, access_id):
        from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
        v = self.value
        return None if v is None else CardCacheEntry(kind=v["kind"], card_revision=v["card_revision"],
                                                     mutation_id=v.get("mutation_id", ""),
                                                     authority=v.get("authority"))

    async def read_in_current_run(self, access_id):
        return True, await self.read(access_id)

    async def restore_projection(self, authority, *, ttl_seconds):
        if self.value is not None and (self.value["kind"] != "card"
                                       or self.value["card_revision"] >= authority.card_revision):
            return False
        self.value = {"kind": "card", "card_revision": authority.card_revision, "authority": authority}
        return True

    async def reconcile_projection(self, *args, **kwargs):
        return False

    async def index_add(self, **kwargs):
        return None

    async def index_remove(self, **kwargs):
        return None


def _served_revision(cache):
    v = cache.value
    return v["card_revision"] if v is not None and v["kind"] == "card" else None


async def _served(tmp_path):
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    service._cache = cache = _LuaCache()
    cache.value = {"kind": "card", "card_revision": before.card_revision, "authority": before}
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
    assert _served_revision(cache) == after.card_revision
    again = await _service_decide(store, service, before, "committed")
    assert again["state"] == "committed"
    assert _served_revision(cache) == after.card_revision


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
    assert _served_revision(cache) == after.card_revision


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
    assert _served_revision(cache) == after.card_revision


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


@pytest.mark.asyncio
async def test_a_writers_precondition_read_refuses_a_staged_card_before_any_side_write(tmp_path):
    # Ops 11:22: writers read current_revision before minting a credential or
    # setting a policy, so the fence there stops side writes, not only the commit.
    store, service, before, after = await _setup(tmp_path)
    assert await service.current_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id) == before.card_revision
    await _stage(store, before, after)
    _record(store, "committed")  # even once PB decided, until it is materialized locally
    with pytest.raises(CardConflict, match="card_transaction_unresolved"):
        await service.current_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    await _decide(store, "committed")
    assert await service.current_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id) == after.card_revision


# ── Ops F8 (11:28): the cached resolver never serves a staged Card's BEFORE ──


def _resolver(store, cache):
    from connection_hub.delegated_credentials.cards.resolver import DelegatedCardResolver
    return DelegatedCardResolver(cache=cache, store=store)


@pytest.mark.asyncio
async def test_a_staged_undecided_card_is_never_served_from_the_cache(tmp_path):
    from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
    from connection_hub.delegated_credentials.cards.service import transaction_mutation_id
    store, service, cache, before, after = await _served(tmp_path)
    assert cache.value["kind"] == "updating" and cache.value["mutation_id"] == transaction_mutation_id(TX)
    resolver = _resolver(store, cache)
    with pytest.raises(CardUnavailable, match="card_updating"):  # P5
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    cache.value = None  # the marker expired: a miss reads durable state, which refuses while undecided
    with pytest.raises((CardUnavailable, CardStorageError)):
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert cache.value is None  # nothing was refilled


@pytest.mark.asyncio
async def test_after_pb_commits_the_cache_never_serves_the_wider_before(tmp_path):
    from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
    store, service, cache, before, after = await _served(tmp_path)
    _record(store, "committed")  # P6: PB decided, the Hub has not materialized it
    resolver = _resolver(store, cache)
    with pytest.raises(CardUnavailable, match="card_updating"):
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    cache.value = None  # marker expired: the durable read follows PB
    served = await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert served.card_revision == after.card_revision


@pytest.mark.asyncio
async def test_a_stage_replay_keeps_its_own_marker(tmp_path):
    from datetime import datetime, timezone
    store, service, cache, before, after = await _served(tmp_path)
    marker = dict(cache.value)
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    assert cache.value == marker


@pytest.mark.asyncio
async def test_a_refused_stage_releases_its_marker(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    store, service, before, after = await _setup(tmp_path)
    service._cache = cache = _LuaCache()
    cache.value = {"kind": "card", "card_revision": before.card_revision, "authority": before}

    async def refuse(*args, **kwargs):
        raise tx.CardTransactionRefused("card_transaction_candidate_invalid")

    monkeypatch.setattr(tx, "stage", refuse)
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    with pytest.raises(tx.CardTransactionRefused):
        await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                        subject_hash=SUBJECT_HASH, original=before, candidate=after, now=when)
    assert _served_revision(cache) is None and cache.value is None  # readers fall through to durable BEFORE


# ── Ops F9 (11:36): a reader restored AFTER before the Hub materialized it ──


@pytest.mark.asyncio
async def test_commit_completes_after_a_reader_restored_after_from_the_recorded_decision(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)
    _record(store, "committed")
    cache.value = None  # the stage marker expired
    served = await _resolver(store, cache).resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=NOW)
    assert served.card_revision == after.card_revision and _served_revision(cache) == after.card_revision
    decided = await _service_decide(store, service, before, "committed")
    assert decided["state"] == "committed" and _served_revision(cache) == after.card_revision
    again = await _service_decide(store, service, before, "committed")
    assert again["state"] == "committed" and _served_revision(cache) == after.card_revision
    # The Card is writable again once materialized.
    assert await service.current_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id) == after.card_revision


@pytest.mark.asyncio
async def test_a_served_revision_with_other_content_does_not_satisfy_the_commit_mark(tmp_path):
    store, service, cache, before, after = await _served(tmp_path)
    _record(store, "committed")
    other = replace(after, label="not this transaction's after")
    cache.value = {"kind": "card", "card_revision": after.card_revision, "authority": other}
    with pytest.raises(CardServingUnavailable):
        await _service_decide(store, service, before, "committed")
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
async def test_a_staged_revoke_completes_over_its_own_restored_tombstone(tmp_path):
    from datetime import datetime, timezone
    from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
    from connection_hub.delegated_credentials.cards.service import replace_state
    store, service, before, _ = await _setup(tmp_path)
    service._cache = cache = _LuaCache()
    cache.value = {"kind": "card", "card_revision": before.card_revision, "authority": before}
    revoked = replace_state(before, CARD_STATE_REVOKED)
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=TX, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=before, candidate=revoked, now=when)
    _record(store, "committed")
    cache.value = {"kind": "revoked", "card_revision": revoked.card_revision}  # served once the marker expired
    decided = await _service_decide(store, service, before, "committed")
    assert decided["state"] == "committed" and cache.value["kind"] == "revoked"


# ── H-R1: recovery can enumerate every in-doubt transaction ────────────────


@pytest.mark.asyncio
async def test_recovery_lists_a_prepared_transaction_until_it_is_decided(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    assert await tx.list_in_doubt(store) == []
    await _stage(store, before, after)
    listed = await tx.list_in_doubt(store)
    assert [(e["transaction_id"], e["state"], e["access_id"]) for e in listed] == [(TX, "prepared", before.access_id)]
    await _decide(store, "aborted")
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_stage_that_crashed_before_its_receipt_is_still_listed(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    original = tx.write_json_atomic

    async def crash_on_receipt(path, payload):
        if payload.get("schema") == tx.TRANSACTION_RECEIPT_SCHEMA:
            raise RuntimeError("killed before the receipt")
        return await original(path, payload)

    monkeypatch.setattr(tx, "write_json_atomic", crash_on_receipt)
    with pytest.raises(RuntimeError):
        await _stage(store, before, after)
    monkeypatch.undo()
    assert await tx.list_in_doubt(store) == [{"transaction_id": TX, "state": "unstaged"}]
    # It holds no fence: an ordinary write proceeds.
    nxt = replace(before, card_revision=before.card_revision + 1, label="ordinary edit")
    await service.commit(nxt, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)


@pytest.mark.asyncio
async def test_too_many_in_flight_transactions_fail_closed(tmp_path, monkeypatch):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    monkeypatch.setattr(tx, "MAX_ACTIVE_TRANSACTIONS", 0)
    with pytest.raises(CardStorageError, match="card_transaction_recovery_queue_unavailable"):
        await tx.list_in_doubt(store)


# ── Hot path (Root and Ops, 11:57): a settled Card costs no coordinator call ──


class _CountingDecisions(Decisions):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def decision(self, receipt):
        self.calls += 1
        return await super().decision(receipt)


def _raw_pointer(store, card):
    import json
    return json.loads(store.current_path(subject_hash=SUBJECT_HASH, access_id=card.access_id).read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_settled_card_is_read_with_no_coordinator_call_and_no_pending_pointer(tmp_path, decision):
    store, _, before, after = await _setup(tmp_path)
    port = _CountingDecisions()
    tx.bind_transaction_decisions(store, port)
    await _stage(store, before, after)
    assert _raw_pointer(store, before)["schema"] == tx.TRANSACTION_POINTER_SCHEMA
    await _decide(store, decision)
    settled = after if decision == "committed" else before
    assert _raw_pointer(store, before).get("schema") != tx.TRANSACTION_POINTER_SCHEMA  # pending pointer retired
    port.calls = 0
    receipt_reads = []
    real_read = tx.read_receipt

    async def counting_read(store_, transaction_id):
        receipt_reads.append(transaction_id)
        return await real_read(store_, transaction_id)

    tx.read_receipt = counting_read
    try:
        for _ in range(3):
            assert await _visible(store, before) == settled
    finally:
        tx.read_receipt = real_read
    assert port.calls == 0 and receipt_reads == []  # Ops N1: no receipt read at all once retired
    assert (await tx.state(store, transaction_id=TX))["state"] == decision  # audit record kept


@pytest.mark.asyncio
async def test_a_decision_replay_retires_a_pointer_a_crash_left_pending(tmp_path, monkeypatch):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)

    async def crash(store_, receipt):
        raise RuntimeError("killed after the decided receipt")

    monkeypatch.setattr(tx, "_retire_pointer", crash)
    with pytest.raises(RuntimeError):
        await _decide(store, "committed")
    monkeypatch.undo()
    assert _raw_pointer(store, before)["schema"] == tx.TRANSACTION_POINTER_SCHEMA
    assert await _visible(store, before) == after  # still correct through the decided receipt
    await _decide(store, "committed")
    assert _raw_pointer(store, before).get("schema") != tx.TRANSACTION_POINTER_SCHEMA


# ── Reader inventory: history and by-name revision reads ───────────────────


async def _history(store, card):
    return await store.list_revision_names(subject_hash=SUBJECT_HASH, access_id=card.access_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_staged_revision_is_out_of_history_until_its_transaction_commits(tmp_path, decision):
    store, _, before, after = await _setup(tmp_path)
    receipt = await _stage(store, before, after)
    staged_name = receipt["after"]["revision_name"]
    assert staged_name not in await _history(store, before)
    assert await store.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                     revision_name=staged_name) is None
    _record(store, decision)
    # History follows the recorded decision, like current reads, even before FINISH.
    assert (staged_name in await _history(store, before)) is (decision == "committed")
    await _decide(store, decision)
    visible = staged_name in await _history(store, before)
    assert visible is (decision == "committed")
    read = await store.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                     revision_name=staged_name)
    assert (read == after) if decision == "committed" else read is None
    assert (await store.read_initial_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)) == before



@pytest.mark.asyncio
async def test_a_replayed_finish_of_an_old_transaction_never_retires_a_newer_pointer(tmp_path):
    # Ops N2: retirement is bound to its own transaction id.
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    await _decide(store, "committed")
    later = replace(after, card_revision=after.card_revision + 1, label="second transaction")
    t2 = "d" * 64
    await _stage(store, after, later, transaction_id=t2)
    assert _raw_pointer(store, before)["transaction_id"] == t2
    await _decide(store, "committed")  # replay of T1's FINISH
    assert _raw_pointer(store, before)["transaction_id"] == t2


@pytest.mark.asyncio
async def test_a_decided_transaction_whose_entry_survived_is_listed_for_finish(tmp_path, monkeypatch):
    # Ops N3: a crash between the decided receipt and its cleanup leaves work to re-drive.
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)

    async def crash(store_, receipt):
        raise RuntimeError("killed after the decided receipt")

    monkeypatch.setattr(tx, "_retire_pointer", crash)
    with pytest.raises(RuntimeError):
        await _decide(store, "committed")
    monkeypatch.undo()
    listed = await tx.list_in_doubt(store)
    assert [(e["transaction_id"], e["state"], e.get("needs_finish")) for e in listed] == [(TX, "committed", True)]
    await _decide(store, "committed")
    assert await tx.list_in_doubt(store) == []

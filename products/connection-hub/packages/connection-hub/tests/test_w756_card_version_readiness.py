"""W756: durable Card visibility waits for every recorded transaction effect.

This is the CardVersion store/readiness leaf, not PB COMMIT or productive policy
composition qualification. The callbacks below expose exact interruption cuts.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable, DelegatedCardResolver
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore, CardStorageError,
)
from test_w661_card_versions import DIGEST, SUBJECT_HASH, TXN, WHEN, _setup


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["second_effect", "before_published_marker", "old_missing_sidecar"])
async def test_pending_effects_never_expose_the_new_card_even_after_restart(tmp_path, monkeypatch, cut):
    store, service, before, after = await _setup(tmp_path)
    effects = [{"kind": "invocation_policy", "key": key} for key in ("one", "two", "three")]
    await service.stage_card_version(
        txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
        members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects,
    )
    stage = await tx.read_card_version_marker(store, TXN)
    revision = stage["members"][0]["revision_name"]
    attempted, finalized = [], []
    real_finalize = service._cache.finalize_removal

    async def finalize(*args, **kwargs):
        finalized.append(kwargs)
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(service._cache, "finalize_removal", finalize)

    async def apply(effect, marker):
        attempted.append(effect["key"])
        if effect["key"] == "two":
            raise CardStorageError("injected_effect_refusal")
        return "applied"

    real_mark = tx._mark_published
    if cut == "before_published_marker":
        async def stop_before_marker(*args, **kwargs):
            raise CardStorageError("injected_marker_cut")
        monkeypatch.setattr(tx, "_mark_published", stop_before_marker)

    with pytest.raises(CardStorageError, match="injected_"):
        await service.publish_card_version(txn=TXN, run_effect=apply)
    monkeypatch.setattr(tx, "_mark_published", real_mark)
    if cut == "old_missing_sidecar":
        # The previously installed publisher deleted this before applying its
        # effects. Recovery must still consult the immutable version record.
        tx.card_version_revision_marker_path(
            store, subject_hash=SUBJECT_HASH, access_id=before.access_id, revision_name=revision,
        ).unlink(missing_ok=True)

    restarted = BundleStorageDelegatedCardStore(tmp_path)
    with pytest.raises(CardStorageError, match="card_version_effects_pending"):
        await restarted.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert await restarted.read_revision(
        subject_hash=SUBJECT_HASH, access_id=before.access_id, revision_name=revision,
    ) is None
    assert revision not in await restarted.list_revision_names(
        subject_hash=SUBJECT_HASH, access_id=before.access_id,
    )
    assert finalized == [], "a failed publication must not clear its serving fence"

    class ReadyCache:
        redis = None  # no cache backend call is made on this proved current-run branch

        def projection_epoch_key(self):
            return "unused-test-epoch"

        def reconcile_lock_key(self):
            return "unused-test-lock"

        async def read_in_current_run(self, access_id):
            # Emulate a legacy ready projection or one surviving a host restart.
            return True, CardCacheEntry(kind="card", authority=after)

    resolver = DelegatedCardResolver(cache=ReadyCache(), store=restarted)
    with pytest.raises(CardUnavailable, match="card_version_effects_pending"):
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    if cut != "before_published_marker":
        with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
            await tx.card_version_outcome(restarted, txn=TXN)

    async def finish(effect, marker):
        attempted.append(effect["key"])
        return "applied"

    await service.publish_card_version(txn=TXN, run_effect=finish)
    assert (await restarted.read_current_authority(
        subject_hash=SUBJECT_HASH, access_id=before.access_id,
    ))[1] == after
    assert await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=int(WHEN.timestamp())) == after
    assert attempted == (["one", "two", "three"] if cut == "before_published_marker"
                         else ["one", "two", "two", "three"])
    assert await tx.read_card_version_marker(store, TXN) is None
    assert not tx.card_version_revision_marker_path(
        store, subject_hash=SUBJECT_HASH, access_id=before.access_id, revision_name=revision,
    ).exists()

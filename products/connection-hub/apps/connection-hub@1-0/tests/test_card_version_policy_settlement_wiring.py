"""Exercise CardVersion recovery through the Hub's real policy-service wiring."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.invocation_policy import InvocationPolicyConflict, POLICY_ALWAYS
from connection_hub.invocation_policy.store import owner_hash_for
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


async def _world(monkeypatch, tmp_path, unavailable, *, successor=False):
    package_tests = Path(__file__).resolve().parents[3] / "packages/connection-hub/tests"
    monkeypatch.syspath_prepend(str(package_tests))
    from test_card_version_policy_settlement import _saved
    from test_w661_card_versions import DIGEST, SUBJECT_HASH, WHEN

    store, cards, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    if successor:
        # A later unrelated Save leaves the older policy marker in place.
        txn = "later-save-" + "b" * 64
        candidate = replace(after, card_revision=after.card_revision + 1, label="later save")
        await cards.stage_card_version(
            txn=txn, request_digest=DIGEST, catalog="catalog-1", now=WHEN + timedelta(seconds=1),
            members=[(SUBJECT_HASH, before.access_id, after.card_revision, candidate)],
        )
        await cards.publish_card_version(txn=txn)
    current = await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    legacy_calls, card_stores = [], []

    def config(_entrypoint):
        legacy_calls.append("config")
        return SimpleNamespace(uses_postgresql=unavailable != "not-postgresql")

    async def persistence(*_args):
        legacy_calls.append("persistence")
        return SimpleNamespace(card_store=store)

    async def callers(*_args):
        legacy_calls.append("callers")
        raise RuntimeError("legacy_callers_unavailable")

    def card_store(root, **kwargs):
        card_stores.append((root, kwargs["lifecycle_lock_scope"]))
        return store

    # Do not replace the settler or _invocation_policy_service: their actual
    # composition is what the package-only regression did not exercise.
    policies._transaction_settler = None
    monkeypatch.setattr(module, "build_invocation_policy_service", lambda **_kwargs: policies)
    monkeypatch.setattr(module, "_storage_root_or_error", lambda _entrypoint: tmp_path)
    monkeypatch.setattr(module, "_lifecycle_lock_scope", lambda _entrypoint: "same-host-flock")
    monkeypatch.setattr(module, "_guarded_card_store", card_store)
    monkeypatch.setattr(module, "_delegated_authority_config", config)
    monkeypatch.setattr(module, "_delegated_card_persistence", persistence)
    monkeypatch.setattr(module, "_card_participant_callers", callers)
    entrypoint = SimpleNamespace(pg_pool=None if unavailable == "no-pool" else object(), redis=object())
    assert module._invocation_policy_service(entrypoint) is policies
    assert card_stores == legacy_calls == []  # marker-free construction stays lazy
    return policies, authority, before, store, current, legacy_calls, card_stores


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["not-postgresql", "no-pool", "callers-unavailable"])
@pytest.mark.parametrize("next_change", ["set", "prepare"])
@pytest.mark.parametrize("successor", [False, True])
async def test_real_wiring_recovers_card_save_without_legacy_authority(
    monkeypatch, tmp_path, unavailable, next_change, successor,
):
    policies, authority, before, store, current, legacy_calls, card_stores = await _world(
        monkeypatch, tmp_path, unavailable, successor=successor,
    )
    kwargs = dict(owner_subject=before.grantor_subject, authority=authority,
                  mode=POLICY_ALWAYS, expected_revision=1)
    if next_change == "set":
        assert (await policies.set_policy(**kwargs)).revision == 2
        assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is None
    else:
        assert (await policies.prepare_policy_change(**kwargs, change_id="next-save")).change_id == "next-save"
    assert legacy_calls == []
    assert card_stores == [(tmp_path, "same-host-flock")]
    assert await store.read_current(subject_hash=owner_hash_for(before.grantor_subject),
                                    access_id=before.access_id) == current


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["prepared", "missing-version"])
async def test_real_wiring_keeps_unproved_card_save_closed(monkeypatch, tmp_path, bad):
    policies, authority, before, store, _current, legacy_calls, card_stores = await _world(
        monkeypatch, tmp_path, "not-postgresql",
    )
    owner = before.grantor_subject
    marker = await policies.get_policy_change(owner_subject=owner, authority=authority)
    if bad == "prepared":
        marker = replace(marker, state="prepared", policy_revision=0, committed_at=0)
        await policies._store.write_policy_change(owner_hash=owner_hash_for(owner), change=marker)
    else:
        async def missing(**_kwargs):
            return None
        monkeypatch.setattr(store, "read_version_record", missing)
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=owner, authority=authority, mode=POLICY_ALWAYS)
    assert await policies.get_policy_change(owner_subject=owner, authority=authority) == marker
    assert legacy_calls == []
    assert card_stores == [(tmp_path, "same-host-flock")]

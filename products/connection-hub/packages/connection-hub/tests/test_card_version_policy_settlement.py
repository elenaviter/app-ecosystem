"""Completed CardVersion Saves must not strand their next policy change."""

from dataclasses import replace
from datetime import timedelta

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_version_port import _port_effect
from connection_hub.delegated_credentials.cards.policy_marker_settlement import CardPolicyMarkerSettler
from connection_hub.invocation_policy import InvocationPolicyConflict, POLICY_ALWAYS
from connection_hub.invocation_policy.store import owner_hash_for
import test_w756_card_version_policies as real
from test_w661_card_versions import DIGEST, SUBJECT_HASH, WHEN

TXN = "pb-save-" + "a" * 64


async def _saved(tmp_path, monkeypatch, *, publish=True):
    monkeypatch.setattr(real, "TXN", TXN)
    store, service, before, after, policies, operation, effects, prepare = await real.setup(tmp_path)
    await service.stage_card_version(
        txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
        members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects, prepare=prepare,
        binding={"caller": "example-caller", "scope": "project:example"},
    )
    if publish:
        await service.publish_card_version(
            txn=TXN, run_effect=_port_effect(operation._apply),
            binding={"caller": "example-caller", "scope": "project:example"},
        )
    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=None, callers={}, max_wait_seconds=0.1,
    ))
    authority = operation._policy_authority(effects[0])
    return store, service, before, after, policies, authority


@pytest.mark.asyncio
@pytest.mark.parametrize("next_change", ["set", "prepare"])
async def test_card_save_then_next_policy_change(tmp_path, monkeypatch, next_change):
    store, _service, before, _after, policies, authority = await _saved(tmp_path, monkeypatch)
    assert await tx.read_card_version_marker(store, TXN) is None
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)).state == "committed"
    if next_change == "set":
        result = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                           mode=POLICY_ALWAYS, expected_revision=1)
        assert result.revision == 2
    else:
        result = await policies.prepare_policy_change(owner_subject=before.grantor_subject, authority=authority,
            mode=POLICY_ALWAYS, expected_revision=1, change_id="next-save")
        assert result.change_id == "next-save"


@pytest.mark.asyncio
@pytest.mark.parametrize("successors", [1, 63, 64])
async def test_save_save_again_then_switch_and_proof_bound(tmp_path, monkeypatch, successors):
    store, service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    current = after
    for index in range(successors):
        txn = "successor-" + f"{index:064x}"
        following = replace(current, card_revision=current.card_revision + 1, label=f"successor {index}")
        await service.stage_card_version(
            txn=txn, request_digest=DIGEST, catalog="catalog-1", now=WHEN + timedelta(seconds=index + 1),
            members=[(SUBJECT_HASH, before.access_id, current.card_revision, following)],
        )
        await service.publish_card_version(txn=txn)
        current = following
    if successors == 64:
        with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
            await policies.set_policy(owner_subject=before.grantor_subject, authority=authority, mode=POLICY_ALWAYS)
        assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is not None
    else:
        saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                          mode=POLICY_ALWAYS, expected_revision=1)
        assert saved.revision == 2
    assert (await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)).card_revision == current.card_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["prepared", "wrong_txn", "wrong_owner", "wrong_access", "wrong_revision",
                                 "missing_record", "wrong_at", "wrong_binding", "pending_txn"])
async def test_unproved_card_version_marker_stays_refused(tmp_path, monkeypatch, bad):
    store, _service, before, _after, policies, authority = await _saved(tmp_path, monkeypatch)
    owner = before.grantor_subject
    marker = await policies.get_policy_change(owner_subject=owner, authority=authority)
    if bad in {"prepared", "wrong_txn", "wrong_owner", "wrong_access", "wrong_revision"}:
        if bad == "prepared":
            marker = replace(marker, state="prepared", committed_at=0, policy_revision=0)
        elif bad == "wrong_txn":
            marker = replace(marker, change_id=f"pb-save-{'b' * 64}:{authority.key}")
        elif bad == "wrong_owner":
            owner = "another-owner"
        elif bad == "wrong_access":
            authority = replace(authority, access_id="another-card")
            marker = replace(marker, authority=authority, change_id=f"{TXN}:{authority.key}")
        else:
            marker = replace(marker, expected_policy_revision=1, policy_revision=2)
        await policies._store.write_policy_change(owner_hash=owner_hash_for(owner), change=marker)
    elif bad == "pending_txn":
        await store.write_marker_document(TXN, {
            "schema": tx.CARD_VERSION_MARKER_SCHEMA, "txn": TXN, "state": "published", "members": [],
            "effects": [{"kind": "invocation_policy"}], "effect_outcomes": {},
        })
    else:
        read_record = store.read_version_record

        async def changed_record(**kwargs):
            record = await read_record(**kwargs)
            if bad == "missing_record":
                return None
            return {**record, "at": "2000-01-01T00:00:00+00:00"} if bad == "wrong_at" else {
                **record, "binding": {"caller": "other", "scope": "other"}}

        monkeypatch.setattr(store, "read_version_record", changed_record)
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=owner, authority=authority, mode=POLICY_ALWAYS)
    assert await policies.get_policy_change(owner_subject=owner, authority=authority) == marker

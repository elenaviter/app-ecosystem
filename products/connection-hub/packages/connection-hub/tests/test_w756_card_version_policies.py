"""Real filesystem CardVersion/policy adapter cuts; not PB COMMIT or PG qualification."""
from types import SimpleNamespace
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore
from connection_hub.delegated_credentials.cards.participant_card_version import CardVersionOperation, CardVersionRefused
from connection_hub.delegated_credentials.cards.store import CardStorageError
from connection_hub.delegated_credentials.cards import transaction_store as tx
from test_invocation_policy import _service as policies_service
from test_w661_card_versions import _setup, TXN, DIGEST, SUBJECT_HASH, WHEN


async def setup(tmp_path):
    store, service, before, after = await _setup(tmp_path / "cards")
    after = replace(after, resource_grants={**after.resource_grants, "service": ("read",)},
                    resource_operations={**after.resource_operations, "service": ("one", "two", "three")})
    policies = policies_service(tmp_path / "policies")
    host = SimpleNamespace(_invocation_policies=policies)
    operation = CardVersionOperation(callers={}, store=ServiceCardVersionStore(service, refused=CardVersionRefused),
        planner=None, host=host, credential_handles=None, nonces=None, clock=lambda: 0)
    # Derive the real effect payload with the production producer, not a browser payload or test callback.
    host._oauth_policy_selection = lambda operations, modes: AutomationAccessService._oauth_policy_selection(
        host, operations, modes)
    effects = await AutomationAccessService._oauth_policy_effects(host, grantor=before.grantor_subject,
        candidate=after, resource_operations={"service": ["one", "two", "three"]},
        invocation_policies={"service": {"one": "once", "two": "always", "three": "once"}})
    effects = [{**effect, "access_id": after.access_id} for effect in effects]

    async def prepare():
        for effect in effects:
            await operation._prepare(TXN, effect)

    return store, service, before, after, policies, operation, effects, prepare


@pytest.mark.asyncio
async def test_three_actual_policy_effects_stay_fenced_until_all_outcomes_then_replay(tmp_path, monkeypatch):
    store, service, before, after, policies, operation, effects, prepare = await setup(tmp_path)
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
        members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects, prepare=prepare)
    for effect in effects:
        authority = operation._policy_authority(effect)
        assert await policies.get(owner_subject=before.grantor_subject, authority=authority) is None
        pending = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
        assert pending.change_id == f"{TXN}:{authority.key}" and pending.state == "prepared"
    commit = policies.commit_policy_change
    attempted = []

    async def cut(**kwargs):
        attempted.append(kwargs["authority"].operation)
        if kwargs["authority"].operation == "three":
            raise OSError("cut before third policy")
        return await commit(**kwargs)

    monkeypatch.setattr(policies, "commit_policy_change", cut)
    from connection_hub.delegated_credentials.cards.card_version_port import _port_effect
    with pytest.raises(CardVersionRefused, match="effects_pending"):
        await service.publish_card_version(txn=TXN, run_effect=_port_effect(operation._apply))
    with pytest.raises(CardStorageError, match="card_version_effects_pending"):
        await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    marker = await tx.read_card_version_marker(store, TXN)
    assert len(marker["effect_outcomes"]) == 1  # sorted one, three, two; the third lexical effect cut
    monkeypatch.setattr(policies, "commit_policy_change", commit)
    await service.publish_card_version(txn=TXN, run_effect=_port_effect(operation._apply))
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == after
    for effect in effects:
        authority = operation._policy_authority(effect)
        policy = await policies.get(owner_subject=before.grantor_subject, authority=authority)
        assert policy.mode == effect["payload"]["mode"] and policy.revision == 1
        assert (await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)).state == "committed"
        # The policy's own exact-change replay does not replenish/advance it.
        assert (await commit(owner_subject=before.grantor_subject, authority=authority,
                             change_id=f"{TXN}:{authority.key}")).revision == 1


@pytest.mark.asyncio
async def test_partial_real_policy_prepare_releases_all_n_on_abort_and_keeps_old_card(tmp_path, monkeypatch):
    store, service, before, after, policies, operation, effects, prepare = await setup(tmp_path)
    real_prepare = policies.prepare_policy_change

    async def cut(**kwargs):
        if kwargs["authority"].operation == "three":
            raise OSError("cut after k of n prepares")
        return await real_prepare(**kwargs)

    monkeypatch.setattr(policies, "prepare_policy_change", cut)
    with pytest.raises(OSError, match="k of n"):
        await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
            members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects, prepare=prepare)
    from connection_hub.delegated_credentials.cards.card_version_port import _port_effect
    await service.rollback_card_version(txn=TXN, release=_port_effect(operation._release))
    for effect in effects:
        authority = operation._policy_authority(effect)
        assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is None
        assert await policies.get(owner_subject=before.grantor_subject, authority=authority) is None
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before

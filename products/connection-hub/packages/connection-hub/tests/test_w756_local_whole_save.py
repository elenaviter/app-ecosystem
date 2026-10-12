"""Local whole Save: real filesystem/policies/coordinator, test decision/catalog ports.

This is not productive PostgreSQL or browser qualification.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
from connection_hub.delegated_credentials.cards.card_participant import DecisionStorePort, PARTICIPANT
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable, DelegatedCardResolver
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
from connection_hub.invocation_policy import InvocationAuthority, SURFACE_OUTER

from test_card_coordinated_writes import _host
from test_card_service import SUBJECT_HASH
from test_invocation_policy import _service as policy_service

RESOURCE = "https://example.test/mcp"
CHOICES = {"one": "once", "three": "always", "two": "once"}


async def _world(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path / "cards")
    policies = policy_service(tmp_path / "policies")
    host._invocation_policies = policies
    after = replace(after, resource_operations={RESOURCE: (*before.resource_operations[RESOURCE], *CHOICES)})
    effects = await host._oauth_policy_effects(grantor=before.grantor_subject, candidate=after,
        resource_operations={RESOURCE: list(CHOICES)}, invocation_policies={RESOURCE: CHOICES})
    participant = host._card_coordinator[0].participants[PARTICIPANT]
    compose_card_effects(card_service=participant._service, card_store=store, grant_store=None, policies=policies)
    return host, store, decisions, before, after, policies, effects


def _authority(card, operation):
    return InvocationAuthority(access_id=card.access_id, resource=RESOURCE,
                               surface=SURFACE_OUTER, operation=operation)


@pytest.mark.asyncio
async def test_three_policies_and_one_card_have_one_local_decision_and_exact_replay(tmp_path):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    await host._persist_record(record_from_card(after), expected_revision=before.card_revision,
                               effects=effects, require_transaction=True)
    assert decisions.decisions == ["committed"] and host._persistence.direct == []
    row = next(iter(decisions.rows.values()))
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == after
    await host._card_coordinator[0].finish(row.transaction_id)
    for operation, mode in CHOICES.items():
        policy = await policies.get(owner_subject=before.grantor_subject, authority=_authority(after, operation))
        assert (policy.mode, policy.revision) == (mode, 1)
    assert decisions.decisions == ["committed"]


@pytest.mark.asyncio
async def test_cut_after_first_policy_fences_current_history_and_ready_cache_across_restart(tmp_path, monkeypatch):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    commit = policies.commit_policy_change
    attempts = []

    async def cut(**kwargs):
        attempts.append(kwargs["authority"].operation)
        if len(attempts) == 2:
            raise OSError("cut second of three policies")
        return await commit(**kwargs)

    monkeypatch.setattr(policies, "commit_policy_change", cut)
    with pytest.raises(CardServingUnavailable, match="card_effects_pending"):
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision,
                                   effects=effects, require_transaction=True)
    assert decisions.decisions == ["committed"]
    row = next(iter(decisions.rows.values()))
    receipt = await tx.read_receipt(store, row.transaction_id)
    assert len(await tx.effect_outcomes(store, row.transaction_id)) == 1
    revision = receipt["after"]["revision_name"]
    restarted = BundleStorageDelegatedCardStore(tmp_path / "cards")
    tx.bind_transaction_decisions(restarted, DecisionStorePort(decisions))
    with pytest.raises(CardStorageError, match="card_effects_pending"):
        await restarted.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert await restarted.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                         revision_name=revision) is None
    assert revision not in await restarted.list_revision_names(subject_hash=SUBJECT_HASH, access_id=before.access_id)

    class ReadyCache:
        redis = None

        def projection_epoch_key(self):
            return "test-only-epoch"

        def reconcile_lock_key(self):
            return "test-only"

        async def read_in_current_run(self, access_id):
            return True, CardCacheEntry(kind="card", authority=after)

    resolver = DelegatedCardResolver(cache=ReadyCache(), store=restarted)
    with pytest.raises(CardUnavailable, match="card_effects_pending"):
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=before.access_id, now=before.created_at)
    monkeypatch.setattr(policies, "commit_policy_change", commit)
    await host._card_coordinator[0].finish(row.transaction_id)
    assert (await restarted.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == after
    assert await restarted.read_revision(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                         revision_name=revision) == after
    assert decisions.decisions == ["committed"]
    for operation in CHOICES:
        assert (await policies.get(owner_subject=before.grantor_subject,
                                   authority=_authority(after, operation))).revision == 1


@pytest.mark.asyncio
async def test_partial_prepare_aborts_and_releases_every_policy_without_advancing_card(tmp_path, monkeypatch):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    prepare = policies.prepare_policy_change
    attempts = []

    async def cut(**kwargs):
        attempts.append(kwargs["authority"].operation)
        if len(attempts) == 2:
            raise OSError("cut partial prepare")
        return await prepare(**kwargs)

    monkeypatch.setattr(policies, "prepare_policy_change", cut)
    with pytest.raises(Exception):
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision,
                                   effects=effects, require_transaction=True)
    assert decisions.decisions == ["aborted"]
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before
    for operation in CHOICES:
        authority = _authority(after, operation)
        assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is None
        assert await policies.get(owner_subject=before.grantor_subject, authority=authority) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("add_policies", [False, True])
async def test_screen_whole_save_uses_existing_local_transaction_without_direct_fallback(tmp_path, add_policies):
    # Keep catalog/resolution ports explicit stand-ins; writes, policy hooks
    # and the single decision are the production implementations above.
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    from test_w756_whole_card_diff import Catalog
    from connection_hub.delegated_credentials.catalog.models import CatalogDocument
    class LocalCatalog(Catalog):
        resource_config = Catalog.card_selector_config

    host._issuer_actor_subject_bound = False
    host._issuer_actor_subject = ""
    host._tenant, host._project = "synthetic", "synthetic"
    host._persistence.load_current = host._persistence.load

    async def active():
        return replace(CatalogDocument.build({}), version=before.catalog_version)

    async def config(*args, **kwargs):
        return LocalCatalog()

    async def resolve(**kwargs):
        return SimpleNamespace(error=None, revoke=False,
            resource_grants=before.resource_grants, resource_operations=kwargs["resource_operations"],
            named_service_operations=before.named_service_operations, named_services=before.named_services,
            operations=before.operations, account_scope=before.account_scope,
            properties=before.properties, identity_scope=before.identity_scope,
            reconciled=SimpleNamespace(to_public_dict=lambda: {}))

    async def notify(*args, **kwargs):
        return None

    host._active_catalog, host._catalog_config = active, config
    host._resolve_card_authority, host.notify_change = resolve, notify
    selection = after.resource_operations if add_policies else before.resource_operations
    answer = await host.update_access({"user_id": before.grantor_subject}, access_id=before.access_id,
        resource_grants=before.resource_grants, resource_operations=selection,
        expected_card_revision=before.card_revision, expected_catalog_version=before.catalog_version,
        label="One local whole Save", _screen_save=True,
        invocation_modes={RESOURCE: CHOICES} if add_policies else {})
    assert answer["ok"] is True, answer
    assert decisions.decisions == ["committed"] and host._persistence.direct == []
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1].label == "One local whole Save"
    if add_policies:
        for operation, mode in CHOICES.items():
            policy = await policies.get(owner_subject=before.grantor_subject, authority=_authority(after, operation))
            assert (policy.mode, policy.revision) == (mode, 1)

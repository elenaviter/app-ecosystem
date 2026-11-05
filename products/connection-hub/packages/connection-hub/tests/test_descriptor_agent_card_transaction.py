"""Descriptor owner Save over the real filesystem participant and decision protocol.

The decision store, catalog resolver and credential-handle port are test ports;
productive PostgreSQL and the SDK credential adapter need separate qualification.
"""

from dataclasses import replace
import time

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.service import CardConflict, CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import CardStorageError, subject_hash_for
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused, CallerWriterRegistry
from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.agent_capability_control import AGENT_DESCRIPTOR_ISSUER_KIND
from connection_hub.delegated_credentials.issuer_gate import IssuerWriteRefused, change_digest
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.catalog.reservations import CatalogReservations
from connection_hub.delegated_credentials.catalog.store import BundleStorageDelegatedCatalogStore
from test_card_coordinated_writes import _host
from test_descriptor_agent_card_save import OWNER, _descriptor_pair


class StoredPersistence:
    def __init__(self, store, handles):
        self.store, self.handles, self.direct = store, handles, []

    async def load(self, access_id, *, subject_hash):
        current = await self.store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        return None if current is None else (current[1], self.handles[access_id])

    load_current = load

    async def current_revision(self, access_id, *, subject_hash):
        current = await self.load(access_id, subject_hash=subject_hash)
        return 0 if current is None else current[0].card_revision

    async def persist(self, authority, *args, **kwargs):
        self.direct.append(authority.access_id)
        raise AssertionError("a descriptor Save must not take the direct path")

    async def persist_guarded(self, *args, **kwargs):
        return await self.persist(*args, **kwargs)


async def _world(tmp_path, *, pair=None):
    service, resident, control = pair if pair is not None else await _descriptor_pair()
    handles = {key: value[1] for key, value in service._persistence.records.items()}
    host, store, decisions, _, _ = await _host(tmp_path / "cards")
    coordinator, intents, _, ttl = host._card_coordinator
    card_service = coordinator.participants[PARTICIPANT]._service
    for card in (control, resident):
        await card_service.commit(card, subject_hash=subject_hash_for(card.grantor_subject),
                                  expected_revision=0, now=int(time.time()))
    catalog_store = BundleStorageDelegatedCatalogStore(tmp_path / "catalog")
    active = await service._active_catalog()
    await catalog_store.write_version(active)
    await catalog_store.publish_active(active)
    tx.bind_catalog_reservations(store, CatalogReservations(catalog_store))
    service._persistence = StoredPersistence(store, handles)
    service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions, intent_ttl_seconds=ttl)
    return service, store, decisions, resident, control, card_service, catalog_store


async def _save(service, resident, **extra):
    return await service.update_access(
        {"user_id": OWNER}, access_id=resident.access_id, resource_grants=resident.resource_grants,
        resource_operations=resident.resource_operations,
        expected_card_revision=resident.card_revision, expected_catalog_version=resident.catalog_version,
        label="Owner's saved label", invocation_modes={}, _screen_save=True, **extra)


async def _current(store, card):
    return (await store.read_current_authority(
        subject_hash=subject_hash_for(card.grantor_subject), access_id=card.access_id))[1]


@pytest.mark.asyncio
async def test_owner_save_commits_locally_with_current_control_and_no_project_call(tmp_path):
    service, store, decisions, resident, control, _, _ = await _world(tmp_path)

    async def project_authorize(*args):
        raise AssertionError("descriptor owner Save must not call a project")

    answer = await _save(service, resident, _authorize_agent_card=project_authorize)
    assert answer["ok"], answer
    assert decisions.decisions == ["committed"] and service._persistence.direct == []
    saved = await _current(store, resident)
    assert saved.label == "Owner's saved label" and saved.card_revision == resident.card_revision + 1
    assert await _current(store, control) == control
    row = next(iter(decisions.rows.values()))
    projection = row.intent.payload["participant_inputs"][PARTICIPANT]
    assert projection["dependency_revisions"][
        f"card:{subject_hash_for(OWNER)}:{control.access_id}"] == control.card_revision
    assert any(key.startswith("catalog-active:") for key in projection["dependency_revisions"])
    await service._card_coordinator[0].finish(row.transaction_id)
    assert decisions.decisions == ["committed"]


@pytest.mark.asyncio
async def test_classification_does_not_exempt_the_generic_direct_writer(tmp_path):
    service, store, decisions, resident, _, _, _ = await _world(tmp_path)
    candidate = replace(resident, card_revision=resident.card_revision + 1, label="Direct writer",
                        resource_operations={"https://example.test/other": ("read",)})
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await service._persist_record(record_from_card(candidate), expected_revision=resident.card_revision)
    assert decisions.decisions == [] and await _current(store, resident) == resident


@pytest.mark.asyncio
async def test_control_moving_between_validation_and_prepare_aborts_owner_save(tmp_path, monkeypatch):
    service, store, decisions, resident, control, card_service, _ = await _world(tmp_path)
    write = service._coordinated_write

    async def race(*args, **kwargs):
        moved = replace(control, card_revision=control.card_revision + 1, label="Concurrent Control edit")
        await card_service.commit(moved, subject_hash=subject_hash_for(OWNER),
                                  expected_revision=control.card_revision, now=int(time.time()))
        return await write(*args, **kwargs)

    monkeypatch.setattr(service, "_coordinated_write", race)
    answer = await _save(service, resident)
    assert answer["ok"] is False and answer["retryable"] is True
    assert decisions.decisions == ["aborted"]
    assert await _current(store, resident) == resident
    assert service._persistence.direct == []


@pytest.mark.asyncio
async def test_control_is_reserved_until_the_local_commit_decision(tmp_path, monkeypatch):
    service, store, decisions, resident, control, card_service, _ = await _world(tmp_path)
    decide = decisions.decide
    checked = []

    async def check(transaction_id, decision, **kwargs):
        if decision == "committed":
            with pytest.raises((CardConflict, CardStorageError), match="reserved"):
                await card_service.commit(replace(control, card_revision=control.card_revision + 1),
                    subject_hash=subject_hash_for(OWNER), expected_revision=control.card_revision, now=int(time.time()))
            checked.append(True)
        return await decide(transaction_id, decision, **kwargs)

    monkeypatch.setattr(decisions, "decide", check)
    answer = await _save(service, resident)
    assert answer["ok"] and checked == [True], answer
    await card_service.commit(replace(control, card_revision=control.card_revision + 1),
        subject_hash=subject_hash_for(OWNER), expected_revision=control.card_revision, now=int(time.time()))


@pytest.mark.asyncio
async def test_new_catalog_before_prepare_refuses_the_old_save(tmp_path):
    service, store, decisions, resident, _, _, catalog_store = await _world(tmp_path)
    changed = CatalogDocument.build({"delegated_credentials": {"oauth": {"enabled": True,
        "resources": [{"resource": "https://example.test/other", "grants": ["use"]}]}}})
    await catalog_store.write_version(changed)
    await catalog_store.publish_active(changed)
    answer = await _save(service, resident)
    assert answer["ok"] is False and answer["retryable"] is True
    assert decisions.decisions == ["aborted"]
    assert await _current(store, resident) == resident


@pytest.mark.asyncio
async def test_interrupted_finish_recovers_the_one_committed_owner_save(tmp_path, monkeypatch):
    service, store, decisions, resident, control, _, _ = await _world(tmp_path)
    coordinator = service._card_coordinator[0]
    finish = coordinator.finish

    async def cut(transaction_id):
        raise CardServingUnavailable("injected_finish_cut")

    monkeypatch.setattr(coordinator, "finish", cut)
    answer = await _save(service, resident)
    assert answer["ok"] is False and decisions.decisions == ["committed"]
    row = next(iter(decisions.rows.values()))
    assert await tx.list_in_doubt(store)
    monkeypatch.setattr(coordinator, "finish", finish)
    await coordinator.finish(row.transaction_id)
    assert (await _current(store, resident)).label == "Owner's saved label"
    assert await _current(store, control) == control
    assert decisions.decisions == ["committed"] and service._persistence.direct == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate_during_gate", [False, True])
async def test_enlisted_descriptor_caller_gate_binds_the_candidate_and_witness(tmp_path, monkeypatch, mutate_during_gate):
    from test_delegated_access_renewal import _Policy
    service, store, decisions, resident, control, _, _ = await _world(tmp_path)
    policy = _Policy(True)
    registry = CallerWriterRegistry()
    registry.register(AGENT_DESCRIPTOR_ISSUER_KIND, policy)
    service.bind_caller_writers(registry)
    candidate = replace(resident, card_revision=resident.card_revision + 1, label="Enlisted edit")
    seen = []
    write = service._coordinated_write
    revalidate = policy.revalidate

    async def capture(record, authority, **kwargs):
        seen.append(authority)
        return await write(record, authority, **kwargs)

    async def recheck(request, initial):
        if mutate_during_gate:
            seen[0].properties["injected_during_gate"] = True
        return await revalidate(request, initial)

    monkeypatch.setattr(service, "_coordinated_write", capture)
    monkeypatch.setattr(policy, "revalidate", recheck)
    active = await service._active_catalog()

    async def save():
        await service._persist_descriptor_agent_edit(
            record_from_card(resident), record_from_card(candidate), control=record_from_card(control),
            active_catalog=active, actor_subject=OWNER)

    if mutate_during_gate:
        with pytest.raises(IssuerWriteRefused, match="issuer_candidate_changed"):
            await save()
        assert decisions.decisions == ["aborted"] and await _current(store, resident) == resident
        assert ("finalize", "refused") in policy.calls
    else:
        await save()
        row = next(iter(decisions.rows.values()))
        assert row.witness_digest == change_digest(candidate.to_dict())
        assert ("decide", "update") in policy.calls and ("revalidate", "update") in policy.calls
        assert ("finalize", "committed") in policy.calls

"""Owned W691 historical public-handler composition controls.

Real signed handler, generic planner, Card version port/service/files, SDK
observed file lock and fixture Redis nonces. Catalog resolution and serving
cache are explicit author fixtures; handle effects use declared stand-ins.
No production PB SQL, Docker-share exclusion, R/P or deployed E2E claim.
Every operation reconstructs its host and store from disk, not a prior plan.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_version_port import ServiceCardVersionStore
from connection_hub.delegated_credentials.cards.locks import guard_card_store_writes, hub_card_mutation_lock
from connection_hub.delegated_credentials.cards.participant_card_version import CardVersionOperation, CardVersionRefused
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import _kdcube_card_mutation_lock
from service_foundation.coordination.durable_wire import canonical_json_bytes

import test_w607_existing_card_selection_plan as selection
import test_w661_card_version_operation as wire
from test_card_service import _Cache


CATALOG = dict(wire.CATALOG)


class _PersistentHost(selection._Host):
    """Only catalog resolution is fake. Every Card read is exact current disk state."""

    def __init__(self, store, *, effects=()):
        super().__init__()
        self.store, self.effects = store, list(effects)

    async def _active_catalog(self):
        return SimpleNamespace(**CATALOG)

    def _cards(self):
        store = self.store

        class CurrentOnly:
            async def load_current(self, access_id, *, subject_hash):
                current = await store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
                return None if current is None else (current[1], current[0])

            def __getattr__(self, name):
                raise AssertionError(f"historical P2 attempted a non-current Card read: {name}")

        return CurrentOnly()

    async def _handle_binding_effects(self, pairs):
        return list(self.effects)

    def __getattr__(self, name):
        if "authoriz" in name or "intent" in name or "transaction" in name or "history" in name:
            raise AssertionError(f"historical P2 attempted a callback or cross-step state: {name}")
        raise AttributeError(name)


class _World:
    def __init__(self, root, redis):
        self.root, self.redis = root, redis
        self.namespace = f"w691-own:{uuid.uuid4().hex}:"

    def storage(self):
        store = BundleStorageDelegatedCardStore(self.root / "cards")
        # Actual historical adapters, synthetic proc role, owned local lock root.
        guard_card_store_writes(store, role=lambda: "proc")
        lock = hub_card_mutation_lock(_kdcube_card_mutation_lock, self.root / "locks", role=lambda: "proc")
        return store, DelegatedCardService(store=store, cache=_Cache(), mutation_lock=lock)

    def operation(self, *, handles=None, effects=(), planner=plan_card_lifecycle):
        store, service = self.storage()

        class Catalog:
            async def read_active(self):
                return SimpleNamespace(**CATALOG)

        port = ServiceCardVersionStore(service, refused=CardVersionRefused, catalog_store=Catalog())
        caller = wire.ParticipantCaller(
            service_id=wire.PEER, request_secret=wire.REQUEST_SECRET, receipt_secret=wire.RECEIPT_SECRET,
            receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
            hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref", plan_scope_prefix="work:project:")
        return CardVersionOperation(
            callers={wire.PEER: caller}, store=port, planner=planner,
            host=_PersistentHost(store, effects=effects), credential_handles=handles or wire._Handles(),
            nonces=self.redis, clock=lambda: wire.NOW, nonce_prefix=self.namespace)

    async def seed(self):
        cards = selection._cards()
        _, service = self.storage()
        for card in cards:
            await service.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0, now=100)
        return cards

    async def current(self, card):
        store, _ = self.storage()
        current = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject), access_id=card.access_id)
        return None if current is None else current[1]

    def files(self):
        # Inventory only this disposable fixture's authority tree, never live storage.
        root = self.root / "cards"
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}


def _edit(card, count=1):
    return {"kind": "reselect", "target_subject": selection.TARGET, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision,
            "selection": {"resource_grants": {"independent-service": [f"unique-edit-{i}:read" for i in range(count)]}}}


async def _call(world, op, *, operation=None, **override):
    defaults = {}
    if op == "stage":
        defaults = {"catalog": CATALOG, "actor_subject": selection.ACTOR, "delegable_grants": ["work:admin"]}
    request = wire._request(op, scope=selection.PROJECT, **{**defaults, **override})
    # Whole transport request and response, not only the result envelope.
    request = json.loads(canonical_json_bytes(request))
    response = json.loads(canonical_json_bytes(await (operation or world.operation()).answer(request)))
    return wire._verified(response, request), response


@pytest_asyncio.fixture
async def world(tmp_path):
    redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        assert await redis.ping()
        yield _World(tmp_path, redis)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 40])
async def test_real_planner_and_fresh_phase_hosts_keep_body_once_and_wire_links_only(world, count):
    _, control, _ = await world.seed()
    baseline = world.files()
    staged, response = await _call(world, "stage", updates=[_edit(control, count)])
    assert staged["kind"] == "staged", staged
    assert await world.current(control) == control
    assert b"unique-edit-0:read" not in canonical_json_bytes(response)
    after_stage = world.files()
    marker = tx.card_version_marker_path(world.storage()[0], wire.TXN)
    marker_bytes = marker.read_bytes()
    assert b"unique-edit-0:read" not in marker_bytes
    assert b'"candidate"' not in marker_bytes and b'"original"' not in marker_bytes
    new_body_files = [path for path, body in after_stage.items() if b"unique-edit-0:read" in body]
    assert len(new_body_files) == 1 and "/revisions/" in new_body_files[0], new_body_files
    staged_files = set(after_stage) - set(baseline)
    sidecar = new_body_files[0].rsplit(".", 1)[0] + ".card-version.json"
    marker_rel = str(marker.relative_to(world.root / "cards"))
    assert staged_files == {new_body_files[0], marker_rel, sidecar}
    assert json.loads(after_stage[sidecar]) == {"txn": wire.TXN}
    assert len(after_stage) == len(baseline) + 3  # one body, one links marker, one txn-only sidecar
    published, published_wire = await _call(world, "publish")
    assert published["kind"] == "published", published
    assert b"unique-edit-0:read" not in canonical_json_bytes(published_wire)
    assert not marker.exists()
    final = world.files()
    assert len(final) == len(baseline) + 1
    assert sidecar not in final
    assert final[new_body_files[0]] == after_stage[new_body_files[0]]  # same exact body, not a second write
    assert (await world.current(control)).card_revision == control.card_revision + 1
    rollback, _ = await _call(world, "rollback", links=staged["members"])
    assert rollback == {"kind": "rollback", "state": "already_published"}
    assert world.files() == final


@pytest.mark.asyncio
async def test_fresh_phase_rollback_removes_exact_unpublished_edit_and_nothing_else(world):
    _, control, _ = await world.seed()
    baseline = world.files()
    staged, _ = await _call(world, "stage", updates=[_edit(control)])
    assert staged["kind"] == "staged", staged
    result, _ = await _call(world, "rollback", links=staged["members"])
    assert result == {"kind": "rollback", "state": "rolled_back"}
    assert world.files() == baseline and await world.current(control) == control


@pytest.mark.asyncio
async def test_real_planner_refusal_returns_no_edit_body_and_writes_nothing(world):
    _, control, _ = await world.seed()
    baseline = world.files()
    edit = _edit(control, 40)
    edit["original_revision"] += 1
    result, response = await _call(world, "stage", updates=[edit])
    assert result == {"kind": "refused", "code": "card_changed", "status": 409}
    assert b"unique-edit-0:read" not in canonical_json_bytes(response)
    assert world.files() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["publish", "rollback"])
async def test_actual_marker_binding_and_full_checksum_survive_phase_reconstruction(world, op):
    _, control, _ = await world.seed()
    staged, _ = await _call(world, "stage", updates=[_edit(control)])
    assert staged["kind"] == "staged"
    snapshot = world.files()
    override = {"scope": "work:project:other"}
    # _request's explicit scope is rebuilt here to avoid helper defaults broadening the signed request.
    request = wire._request(op, **override, **({"links": staged["members"]} if op == "rollback" else {}))
    response = await world.operation().answer(request)
    assert wire._verified(response, request) == {"kind": "refused", "code": "request_scope_invalid", "status": 403}
    assert world.files() == snapshot


@pytest.mark.asyncio
async def test_real_handler_recreates_existing_person_cards_on_stable_ids_with_current_fences(world):
    project, control, my = await world.seed()
    creations = [
        {"ref": "control", "kind": "project_person_control", "identity": {"target_subject": selection.TARGET},
         "selection": {}, "parent": {"access_id": project.access_id, "holder_subject": project.grantor_subject}},
        {"ref": "my", "kind": "project_person_my_card", "identity": {"person_subject": selection.TARGET},
         "selection": {}, "parent": {"ref": "control"}},
    ]
    staged, _ = await _call(world, "stage", updates=[], creations=creations,
                            project_control={"control_id": project.access_id, "holder_subject": project.grantor_subject})
    assert staged["kind"] == "staged", staged
    assert {m["card"]["access_id"]: m["base_version"] for m in staged["members"]} == {
        control.access_id: control.card_revision, my.access_id: my.card_revision}
    assert (await _call(world, "publish"))[0]["kind"] == "published"
    for before in (control, my):
        after = await world.current(before)
        assert after.access_id == before.access_id and after.card_revision == before.card_revision + 1


@pytest.mark.asyncio
async def test_cancelled_real_store_publish_drains_started_handle_before_local_flocks_release(world):
    _, control, _ = await world.seed()
    started, release_write = asyncio.Event(), asyncio.Event()
    order = []
    row = {"from_identity": "synthetic-row", "from_revision": control.card_revision,
           "from_expires_at": 0, "from_fingerprint": ""}

    class SlowHandles(wire._Handles):
        async def advance_binding(self, access_id, **kwargs):
            started.set()
            await release_write.wait()
            order.append("handle_finished")
            return "applied"

    handles = SlowHandles(identity=row)
    effect = {"kind": "handle_binding", "key": f"handle:{control.access_id}", "payload": {
        **row, "access_id": control.access_id, "prepared_at": 0,
        "card_revision": control.card_revision + 1, "expires_at": 0}}
    staged, _ = await _call(world, "stage", updates=[_edit(control)],
                            operation=world.operation(handles=handles, effects=[effect]))
    assert staged["kind"] == "staged", staged
    task = asyncio.create_task(_call(world, "publish", operation=world.operation(handles=handles, effects=[effect])))
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancel must not release the real store's locks before a started handle write ends"
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        order.append("handler_returned")
        assert order == ["handle_finished", "handler_returned"]
        result, _ = await _call(world, "rollback", links=staged["members"],
                                operation=world.operation(handles=handles, effects=[effect]))
        assert result == {"kind": "rollback", "state": "already_published"}
    finally:
        release_write.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

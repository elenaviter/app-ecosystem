"""Isolated helper checks, not qualification of production target adapters."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_POINTER_SCHEMA
from connection_hub.delegated_credentials.cards.participant_effects import (
    ParticipantEffectApplier, ParticipantEffectRefused,
)
from connection_hub.invocation_policy.models import InvocationAuthority

TX = "a" * 64


def receipt():
    before = {"schema": CARD_POINTER_SCHEMA, "access_id": "card_1", "card_revision": 1,
              "revision_name": "revision_1", "content_hash": "b" * 64,
              "updated_at": "2026-10-06T12:00:00Z", "expires_at": 100, "state": "active"}
    return {"transaction_id": TX, "state": "committed", "intent_digest": "c" * 64,
            "participant": "hub", "subject_hash": hashlib.sha256(b"synthetic-owner").hexdigest(), "access_id": "card_1",
            "change_digest": "e" * 64, "before": before,
            "after": {**before, "card_revision": 2, "revision_name": "revision_2",
                      "content_hash": "f" * 64, "expires_at": 200},
            "effects": [{"kind": "credential_lifetime", "key": "access",
                         "payload": {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1}}]}


class SyntheticTarget:
    def __init__(self):
        self.calls = []
        self.applied = {}

    async def apply_once(self, binding, payload):
        self.calls.append((binding, deepcopy(payload)))
        identity = (binding.transaction_id, binding.kind, binding.key)
        previous = self.applied.setdefault(identity, (binding.receipt_digest, binding.effect_digest))
        if previous != (binding.receipt_digest, binding.effect_digest):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest


def harness(saved=None):
    saved = receipt() if saved is None else saved
    target = SyntheticTarget()
    async def read(transaction_id):
        assert transaction_id == TX
        return deepcopy(saved)
    return saved, target, ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": target})


async def invoke(applier, payload=None):
    await applier("credential_lifetime", "access", payload or {"access_id": "card_1", "expires_at": 200,
                                                             "base_card_revision": 1},
                  transaction_id=TX)


@pytest.mark.asyncio
async def test_exact_replay_keeps_binding_and_absolute_deadline():
    _, target, applier = harness()
    await invoke(applier)
    await invoke(applier)
    assert len(target.applied) == 1
    assert target.calls[0] == target.calls[1]
    assert target.calls[0][1]["expires_at"] == 200  # no now() or TTL recomputation


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "aborted", "unknown", None])
async def test_zero_effects_without_committed_decision(state):
    saved, target, applier = harness()
    saved["state"] = state
    with pytest.raises(ParticipantEffectRefused, match="card_effect_not_committed"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"expires_at": 201}, "card_effect_digest_mismatch"),
    ({"access_id": "later_card"}, "card_effect_payload_binding_invalid"),
    ({"expires_at": True}, "card_effect_deadline_invalid"),
    ({"ttl_seconds": 100}, "card_effect_payload_binding_invalid"),
    ({"access_token": "synthetic-forbidden-value"}, "card_effect_secret_field"),
])
async def test_payload_change_refuses_before_target(change, reason):
    _, target, applier = harness()
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await invoke(applier, {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1, **change})
    assert target.calls == []


@pytest.mark.asyncio
async def test_duplicate_effect_is_not_a_partial_application():
    saved, target, applier = harness()
    saved["effects"].append(deepcopy(saved["effects"][0]))
    with pytest.raises(ParticipantEffectRefused, match="card_effect_set_invalid"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
async def test_later_receipt_vector_cannot_reuse_target_identity():
    saved, target, applier = harness()
    await invoke(applier)
    saved["after"]["content_hash"] = "1" * 64
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await invoke(applier)
    assert len(target.applied) == 1


@pytest.mark.asyncio
async def test_adapter_absence_fails_closed():
    async def read(tx):
        return receipt()
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await invoke(ParticipantEffectApplier(read_receipt=read, targets={}))


@pytest.mark.asyncio
async def test_boolean_revision_is_not_an_integer_revision():
    saved, target, applier = harness()
    saved["before"]["card_revision"] = True
    with pytest.raises(ParticipantEffectRefused, match="card_effect_receipt_binding_invalid"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
async def test_unhashable_policy_mode_is_a_named_refusal():
    saved, target, applier = harness()
    saved["effects"] = [{"kind": "invocation_policy", "key": "resource/operation",
                         "payload": {"owner_subject": "synthetic-owner", "mode": [], "expected_revision": 0,
                                     "authority": {"access_id": "card_1", "resource": "resource",
                                                   "operation": "operation", "surface": "outer"}}}]
    with pytest.raises(ParticipantEffectRefused, match="card_effect_payload_invalid"):
        await applier("invocation_policy", "resource/operation", saved["effects"][0]["payload"],
                      transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
async def test_non_policy_prepare_and_release_are_validated_noops():
    saved, target, applier = harness()
    effect = saved["effects"][0]
    saved["state"] = "prepared"
    await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    saved["state"] = "aborted"
    await applier.release(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
async def test_policy_phase_identity_is_stable_across_decision():
    saved = receipt()
    saved["effects"] = [{"kind": "invocation_policy", "key": "resource/operation", "payload": {
        "owner_subject": "synthetic-owner", "mode": "always", "expected_revision": 0,
        "authority": {"access_id": "card_1", "resource": "resource", "operation": "operation", "surface": "outer"}}}]
    saved["effects"][0]["key"] = InvocationAuthority.from_mapping(saved["effects"][0]["payload"]["authority"]).key
    bindings = []
    class Target:
        async def prepare_once(self, binding, payload):
            bindings.append(binding)
            return binding.effect_digest
        async def release_once(self, binding, payload):
            bindings.append(binding)
            return binding.effect_digest
    async def read(tx):
        return deepcopy(saved)
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": Target()})
    effect = saved["effects"][0]
    saved["state"] = "prepared"
    saved["reason"] = ""
    await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    saved["state"] = "aborted"
    saved["reason"] = "synthetic abort"
    await applier.release(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert bindings[0] == bindings[1]
    assert "state" not in bindings[0].receipt()


def effect_for(kind):
    payloads = {
        "grant_binding": {"access_id": "card_1", "operations": ["read"], "resource_grants": {},
                          "resource_operations": {}, "named_services": {}, "expires_at": 200, "slot": "new"},
        "credential_lifetime": {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1},
        "invocation_policy": {"owner_subject": "synthetic-owner", "mode": "always", "expected_revision": 0,
                              "authority": {"access_id": "card_1", "resource": "resource", "operation": "read",
                                            "surface": "outer", "account": {"provider_id": "synthetic", "account_id": "account_1"}}},
        "grant_unbind": {"access_id": "card_1", "session_id": "synthetic-session", "token_sha256": "2" * 64},
    }
    payload = payloads[kind]
    key = {"grant_binding": "new", "credential_lifetime": "card", "grant_unbind": "old"}.get(kind)
    if key is None:
        key = InvocationAuthority.from_mapping(payload["authority"]).key
    return {"kind": kind, "key": key, "payload": payload}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant_binding", "credential_lifetime", "invocation_policy", "grant_unbind"])
async def test_every_kind_dispatches_only_its_exact_bound_payload(kind):
    saved = receipt()
    effect = effect_for(kind)
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return deepcopy(saved)
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: target})
    result = await applier.apply(kind, effect["key"], effect["payload"], transaction_id=TX)
    assert result == target.calls[0][0].effect_digest
    assert target.calls[0][1] == effect["payload"]


@pytest.mark.asyncio
async def test_named_lifetime_noop_reaches_public_callback_unchanged():
    saved = receipt()
    class Target:
        async def apply_once(self, binding, payload):
            return "no_active_credentials"
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    effect = saved["effects"][0]
    assert await applier(effect["kind"], effect["key"], effect["payload"], transaction_id=TX) == "no_active_credentials"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, False, True, 1, "", "applied", "3" * 64])
async def test_unproven_target_success_is_refused(result):
    saved = receipt()
    class Target:
        async def apply_once(self, binding, payload):
            return result
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_applied_receipt_invalid"):
        await invoke(applier)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant_binding", "invocation_policy", "grant_unbind"])
async def test_lifetime_noop_cannot_claim_another_kind_applied(kind):
    saved = receipt()
    effect = effect_for(kind)
    saved["effects"] = [effect]
    class Target:
        async def apply_once(self, binding, payload):
            return "no_active_credentials"
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: Target()})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_applied_receipt_invalid"):
        await applier(kind, effect["key"], effect["payload"], transaction_id=TX)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["reader", "target"])
@pytest.mark.parametrize("exception", [ValueError, ParticipantEffectRefused])
async def test_external_exception_text_is_never_exposed(source, exception):
    import traceback
    sentinel = "synthetic-secret-not-for-output"
    async def read(tx):
        if source == "reader":
            raise exception(sentinel)
        return receipt()
    class Target:
        async def apply_once(self, binding, payload):
            raise exception(sentinel)
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    with pytest.raises(ParticipantEffectRefused) as caught:
        await invoke(applier)
    assert sentinel not in str(caught.value)
    assert sentinel not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0, 1, 200])
async def test_past_or_zero_absolute_deadlines_are_not_extended(deadline):
    saved, target, applier = harness()
    saved["effects"][0]["payload"]["expires_at"] = deadline
    await invoke(applier, saved["effects"][0]["payload"])
    assert target.calls[0][1]["expires_at"] == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"owner_subject": "another-owner"}, "card_effect_owner_mismatch"),
    ({"expected_revision": True}, "card_effect_payload_invalid"),
    ({"authority": {"access_id": "other", "resource": "resource", "operation": "read", "surface": "outer"}}, "card_effect_policy_binding_invalid"),
])
async def test_policy_actor_target_and_revision_are_bound(change, reason):
    saved = receipt()
    saved["state"] = "prepared"
    effect = effect_for("invocation_policy")
    effect["payload"].update(change)
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": target})
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key,reason", [("x" * 192, "card_effect_key_invalid"), ("truncated-key", "card_effect_policy_binding_invalid")])
async def test_policy_key_is_exact_and_oversized_key_is_refused_at_prepare(key, reason):
    saved = receipt()
    saved["state"] = "prepared"
    effect = effect_for("invocation_policy")
    effect["key"] = key
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": target})
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await applier.prepare(effect["kind"], key, effect["payload"], transaction_id=TX)
    assert target.calls == []


@pytest.fixture
def transaction_core():
    """Joint checks require the explicit writer dependency, not a mock core.

    On the pre-participant integration base this dependency is absent. Those
    skips are not qualifying evidence: run with the exact writer source plus
    this helper source, and report both heads and zero skipped joint checks.
    """
    import importlib.util
    name = "connection_hub.delegated_credentials.cards.transaction_store"
    if importlib.util.find_spec(name) is None:
        pytest.skip("joint recovery requires the exact Card transaction-store dependency")
    return __import__(name, fromlist=["transaction_store"])


class SQLiteSyntheticTarget:
    """Synthetic atomic target/identity ledger, not the production token store."""
    def __init__(self, root, *, crash_point=None, outcome=None):
        self.path = Path(root) / "synthetic-target.sqlite"
        self.crash_point = crash_point
        self.outcome = outcome

    async def apply_once(self, binding, payload):
        import sqlite3
        if self.crash_point == "before_target":
            _stop_for_kill()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS effects (tx TEXT, kind TEXT, effect_key TEXT, receipt TEXT, digest TEXT, result TEXT, deadline INTEGER, PRIMARY KEY (tx, kind, effect_key))")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT receipt,digest,result FROM effects WHERE tx=? AND kind=? AND effect_key=?",
                             (binding.transaction_id, binding.kind, binding.key)).fetchone()
            if row is None:
                result = self.outcome or binding.effect_digest
                db.execute("INSERT INTO effects VALUES (?,?,?,?,?,?,?)", (binding.transaction_id, binding.kind,
                           binding.key, binding.receipt_digest, binding.effect_digest, result, payload.get("expires_at")))
            else:
                if row[:2] != (binding.receipt_digest, binding.effect_digest):
                    raise ParticipantEffectRefused("card_effect_binding_mismatch")
                result = row[2]
            db.commit()
        if (self.crash_point == "after_target"
                or self.crash_point == "after_second_target" and binding.kind == "grant_unbind"):
            _stop_for_kill()
        return result


def _stop_for_kill():
    import os
    import signal
    print("synthetic-checkpoint", flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)


def _effect_crash_worker(root, point):
    """Child stops at a precise durable boundary; its parent sends SIGKILL."""
    import asyncio
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    store = BundleStorageDelegatedCardStore(Path(root))
    async def read(transaction_id):
        return await tx.read_receipt(store, transaction_id)
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: SQLiteSyntheticTarget(store.root, crash_point=point)
        for kind in ("credential_lifetime", "grant_unbind")})
    atomic_write = tx.write_json_atomic
    async def write(path, value):
        await atomic_write(path, value)
        if point == "after_marker" and path == tx.effects_path(store, TX):
            _stop_for_kill()
    tx.write_json_atomic = write
    async def finish():
        saved = await tx.read_receipt(store, TX)
        await tx.apply_effects(store, saved, applier)
    asyncio.run(finish())


def _kill_at(root, point):
    import os
    import select
    import subprocess
    import sys
    own_cards = Path(__file__).resolve().parents[1] / "src/connection_hub/delegated_credentials/cards"
    script = ("import connection_hub.delegated_credentials.cards as cards; "
              f"cards.__path__.append({str(own_cards)!r}); "
              "from test_participant_effects import _effect_crash_worker; "
              f"_effect_crash_worker({str(root)!r}, {point!r})")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent), env.get("PYTHONPATH", "")])
    child = subprocess.Popen([sys.executable, "-c", script], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, "child did not reach the bounded crash checkpoint"
        line = child.stdout.readline().strip()
        assert line == "synthetic-checkpoint", child.communicate(timeout=5)[1] if child.poll() is not None else line
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode == -9
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


async def _joint_setup(root, tx):
    from datetime import datetime, timezone
    from connection_hub.delegated_credentials.cards.model import card_revision_name
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from connection_hub.delegated_credentials.durable_io import write_json_atomic
    store = BundleStorageDelegatedCardStore(root)
    saved = {**receipt(), "schema": tx.TRANSACTION_RECEIPT_SCHEMA, "reason": ""}
    for side in ("before", "after"):
        pointer = saved[side]
        pointer["revision_name"] = card_revision_name(card_revision=pointer["card_revision"],
            content_hash=pointer["content_hash"], updated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
    await write_json_atomic(tx.receipt_path(store, TX), saved)
    await write_json_atomic(tx.active_path(store, TX), {"transaction_id": TX})
    await write_json_atomic(tx.marker_path(store, subject_hash=saved["subject_hash"], access_id="card_1"),
                            {"transaction_id": TX})
    await write_json_atomic(store.current_path(subject_hash=saved["subject_hash"], access_id="card_1"),
                            {"schema": tx.TRANSACTION_POINTER_SCHEMA, "transaction_id": TX,
                             "before": saved["before"], "after": saved["after"]})
    return store, saved


async def _recover_effects(store, tx, *, outcome=None):
    async def read(transaction_id):
        return await tx.read_receipt(store, transaction_id)
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: SQLiteSyntheticTarget(store.root, outcome=outcome)
        for kind in ("credential_lifetime", "grant_unbind")})
    saved = await tx.read_receipt(store, TX)
    await tx.apply_effects(store, saved, applier)


def _target_rows(root):
    import sqlite3
    path = Path(root) / "synthetic-target.sqlite"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute("SELECT receipt,digest,result,deadline FROM effects").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["before_target", "after_target", "after_marker"])
async def test_sigkill_finish_recovers_exact_target_once_and_keeps_fence_until_ready(tmp_path, transaction_core, point):
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    tx = transaction_core
    store, saved = await _joint_setup(tmp_path, tx)
    _kill_at(tmp_path, point)
    pending = await tx.pending_effects(store, saved)
    rows = _target_rows(store.root)
    assert len(rows) == (0 if point == "before_target" else 1)
    if point != "after_marker":
        assert pending
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await tx.assert_replaceable(store, subject_hash=saved["subject_hash"], access_id="card_1")
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await tx.resolve_pointer(store, {"schema": tx.TRANSACTION_POINTER_SCHEMA, "transaction_id": TX,
                "before": saved["before"], "after": saved["after"]},
                subject_hash=saved["subject_hash"], access_id="card_1")
    else:
        assert pending == []  # target is durable; only pointer/index retirement is unfinished
    assert await tx.list_in_doubt(store)
    await _recover_effects(store, tx)
    await _recover_effects(store, tx)
    assert len(_target_rows(store.root)) == 1
    assert _target_rows(store.root)[0][3] == 200  # past absolute expiry remains unchanged on restart
    assert await tx.pending_effects(store, saved) == []
    assert await tx.list_in_doubt(store) == []
    await tx.assert_replaceable(store, subject_hash=saved["subject_hash"], access_id="card_1")
    assert (await tx.effect_outcomes(store, TX))["0"] == _target_rows(store.root)[0][1]


@pytest.mark.asyncio
async def test_two_consecutive_finish_crashes_keep_readiness_fenced(tmp_path, transaction_core):
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    tx = transaction_core
    store, saved = await _joint_setup(tmp_path, tx)
    for _ in range(2):
        _kill_at(tmp_path, "after_target")
        assert len(_target_rows(store.root)) == 1
        assert await tx.list_in_doubt(store)
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await tx.assert_replaceable(store, subject_hash=saved["subject_hash"], access_id="card_1")
    await _recover_effects(store, tx)
    assert await tx.list_in_doubt(store) == []
    assert len(_target_rows(store.root)) == 1


@pytest.mark.asyncio
async def test_named_noop_is_durable_in_real_effects_record_and_replay(tmp_path, transaction_core):
    tx = transaction_core
    store, _ = await _joint_setup(tmp_path, tx)
    await _recover_effects(store, tx, outcome="no_active_credentials")
    assert await tx.effect_outcomes(store, TX) == {"0": "no_active_credentials"}
    await _recover_effects(store, tx)  # later availability cannot change a recorded no-op
    assert await tx.effect_outcomes(store, TX) == {"0": "no_active_credentials"}
    assert _target_rows(store.root)[0][2] == "no_active_credentials"


@pytest.mark.asyncio
async def test_partial_effect_completion_does_not_release_readiness(tmp_path, transaction_core):
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    from connection_hub.delegated_credentials.durable_io import write_json_atomic
    tx = transaction_core
    store, saved = await _joint_setup(tmp_path, tx)
    saved["effects"].append(effect_for("grant_unbind"))
    await write_json_atomic(tx.receipt_path(store, TX), saved)
    _kill_at(tmp_path, "after_second_target")
    assert len(_target_rows(store.root)) == 2
    assert [index for index, _ in await tx.pending_effects(store, saved)] == [1]
    with pytest.raises(CardStorageError, match="card_effects_pending"):
        await tx.assert_replaceable(store, subject_hash=saved["subject_hash"], access_id="card_1")
    await _recover_effects(store, tx)
    assert len(_target_rows(store.root)) == 2
    assert await tx.pending_effects(store, saved) == []
    assert await tx.list_in_doubt(store) == []

"""W578: a disconnect deletes exactly the account connection its decision named, never a later reconnection.

Account ids are deterministic and ``credential_id`` is derived from them, so a
reconnection after a disconnect would otherwise look identical. Each stored
connection carries a random incarnation, kept while the record exists and
minted anew after it is gone; the ``account_delete`` effect names it.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import AccountDeleteTarget
from connection_hub.delegated_credentials.cards.participant_effects import (
    EffectBinding, ParticipantEffectRefused, _payload,
)
from connection_hub.delegated_to_kdcube.models import ConnectedAccount
from connection_hub.delegated_to_kdcube.store import (
    AccountDisconnectPending, AccountLockUnavailable, DelegatedToKdcubeStore,
)
from test_account_store import _MemoryUserConfiguration

GRANTOR = "user-1"


class _AccountLocks:
    """One lock per (user, account), as the composition's shared lock provides."""

    def __init__(self):
        self.locks = {}

    @asynccontextmanager
    async def __call__(self, user_id, account_id):
        lock = self.locks.setdefault((user_id, account_id), asyncio.Lock())
        async with lock:
            yield


def _store(backend=None, locks=None) -> DelegatedToKdcubeStore:
    return DelegatedToKdcubeStore(user_id=GRANTOR, backend=backend or _MemoryUserConfiguration(),
                                  account_lock=locks or _AccountLocks())


def _account(**changes) -> ConnectedAccount:
    values = dict(account_id="account-1", provider_id="google", claims=("mail.read",))
    values.update(changes)
    return ConnectedAccount(**values)


@pytest.mark.asyncio
async def test_a_connection_keeps_its_incarnation_through_updates_and_gets_a_new_one_after_a_disconnect():
    store = _store()
    first = await store.upsert_account(_account())
    assert len(first.incarnation) == 32
    again = await store.upsert_account(_account(display_name="renamed"))  # a reconnect without disconnect
    assert again.incarnation == first.incarnation
    status = await store.set_account_status("account-1", "revoked")
    assert status.incarnation == first.incarnation
    assert await store.disconnect_account("account-1")
    reconnected = await store.upsert_account(_account(incarnation=first.incarnation))  # a stale carried one
    assert reconnected.credential_id == first.credential_id  # deterministic: no proof of identity
    assert reconnected.incarnation and reconnected.incarnation != first.incarnation


@pytest.mark.asyncio
async def test_a_legacy_account_without_an_incarnation_gets_one_once():
    backend = _MemoryUserConfiguration()
    store = _store(backend)
    await store.upsert_account(_account())
    key = (GRANTOR, store.bundle_id, store.account_prop_key("account-1"))
    backend.props[key] = {**backend.props[key], "incarnation": ""}
    minted = await store.ensure_incarnation("account-1")
    assert len(minted) == 32 and await store.ensure_incarnation("account-1") == minted
    assert await store.ensure_incarnation("no-such-account") == ""


@pytest.mark.asyncio
async def test_disconnect_incarnation_deletes_only_the_named_connection_and_pins_its_outcome():
    store = _store()
    first = await store.upsert_account(_account())
    assert await store.disconnect_incarnation("account-1", "f" * 32, pin="p-other") == "account_reconnected"
    assert await store.get_account("account-1") is not None
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "disconnected"
    assert await store.get_account("account-1") is None
    later = await store.upsert_account(_account())
    # A delayed replay of the same decision returns its pinned outcome and never removes the reconnection.
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "disconnected"
    assert (await store.get_account("account-1")).incarnation == later.incarnation


@pytest.mark.asyncio
async def test_an_absent_outcome_stays_absent_after_a_reconnect():
    """CodeApp: account_absent then a reconnect must not flip the same old replay to account_reconnected."""
    store = _store()
    first = await store.upsert_account(_account())
    await store.disconnect_account("account-1")
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "account_absent"
    await store.upsert_account(_account())
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "account_absent"


@pytest.mark.asyncio
async def test_a_crash_between_the_pin_and_the_deletion_finishes_on_replay():
    backend = _MemoryUserConfiguration()
    store = _store(backend)
    first = await store.upsert_account(_account())
    # The state a crash leaves after the "deleting" pin and before the deletion.
    await store._set_prop(store.account_delete_pin_key("p-1"),
                          {"state": "deleting", "account_id": "account-1", "incarnation": first.incarnation})
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "disconnected"
    assert await store.get_account("account-1") is None


@pytest.mark.asyncio
async def test_a_crash_after_the_deletion_before_the_final_pin_reports_the_deletion_not_an_absence():
    backend = _MemoryUserConfiguration()
    store = _store(backend)
    first = await store.upsert_account(_account())
    await store._set_prop(store.account_delete_pin_key("p-1"),
                          {"state": "deleting", "account_id": "account-1", "incarnation": first.incarnation})
    await store.disconnect_account("account-1")  # the deletion landed, then the process died
    await store.upsert_account(_account())  # and the account was reconnected before recovery
    assert await store.disconnect_incarnation("account-1", first.incarnation, pin="p-1") == "disconnected"
    assert await store.get_account("account-1") is not None  # the reconnection is untouched


@pytest.mark.asyncio
async def test_a_reconnect_racing_the_deletion_is_ordered_by_the_account_lock():
    locks = _AccountLocks()
    store = _store(locks=locks)
    first = await store.upsert_account(_account())
    deletion_inside = asyncio.Event()
    release = asyncio.Event()
    real_disconnect = store._disconnect_account

    async def slow_disconnect(account_id, **kwargs):
        deletion_inside.set()
        await release.wait()
        return await real_disconnect(account_id, **kwargs)

    store._disconnect_account = slow_disconnect
    deleting = asyncio.create_task(store.disconnect_incarnation("account-1", first.incarnation, pin="p-1"))
    await deletion_inside.wait()
    reconnecting = asyncio.create_task(store.upsert_account(_account()))
    await asyncio.sleep(0.05)
    assert not reconnecting.done()  # the reconnect waits for the deletion's account section
    release.set()
    assert await deleting == "disconnected"
    reconnected = await reconnecting
    assert reconnected.incarnation != first.incarnation and await store.get_account("account-1") is not None


@pytest.mark.asyncio
async def test_without_the_shared_account_lock_the_incarnation_operations_refuse():
    store = DelegatedToKdcubeStore(user_id=GRANTOR, backend=_MemoryUserConfiguration())
    await store.upsert_account(_account())  # ordinary writes keep working
    with pytest.raises(AccountLockUnavailable):
        await store.ensure_incarnation("account-1")
    with pytest.raises(AccountLockUnavailable):
        await store.disconnect_incarnation("account-1", "a" * 32, pin="p-1")


def _effect_payload(**changes):
    payload = {"access_id": "card-1", "grantor_subject": GRANTOR, "provider_id": "google",
               "account_id": "account-1", "incarnation": "a" * 32}
    payload.update(changes)
    return payload


SUBJECT_HASH = hashlib.sha256(GRANTOR.encode("utf-8")).hexdigest()


def test_the_account_delete_payload_is_bound_to_the_card_the_grantor_and_its_key():
    _payload("account_delete", "google:account-1", _effect_payload(), "card-1", 1, SUBJECT_HASH)
    for kind_key, payload, subject in (
            ("google:account-1", _effect_payload(access_id="other-card"), SUBJECT_HASH),
            ("google:account-1", _effect_payload(grantor_subject="someone-else"), SUBJECT_HASH),
            ("google:other", _effect_payload(), SUBJECT_HASH),
            ("google:account-1", _effect_payload(incarnation=""), SUBJECT_HASH),
            ("google:account-1", {**_effect_payload(), "extra": 1}, SUBJECT_HASH)):
        with pytest.raises(ParticipantEffectRefused):
            _payload("account_delete", kind_key, payload, "card-1", 1, subject)


def _binding() -> EffectBinding:
    return EffectBinding("t" * 64, "account_delete", "google:account-1", "e" * 64, "r" * 64, "{}")


@pytest.mark.asyncio
async def test_the_target_deletes_the_named_incarnation_and_every_outcome_is_final():
    store = _store()
    first = await store.upsert_account(_account())
    target = AccountDeleteTarget(lambda grantor: store if grantor == GRANTOR else None)
    payload = _effect_payload(incarnation=first.incarnation)
    assert await target.apply_once(_binding(), payload) == "e" * 64
    assert await store.get_account("account-1") is None
    assert await target.apply_once(_binding(), payload) == "e" * 64  # replay: absent, same answer
    await store.upsert_account(_account())
    assert await target.apply_once(_binding(), payload) == "e" * 64  # reconnected: left as it is
    assert await store.get_account("account-1") is not None


@pytest.mark.asyncio
async def test_without_an_account_store_the_target_refuses_by_name():
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await AccountDeleteTarget(None).apply_once(_binding(), _effect_payload())


@pytest.mark.asyncio
async def test_a_staged_hold_refuses_a_reconnect_until_the_decision_and_abort_releases_it():
    store = _store()
    first = await store.upsert_account(_account())
    target = AccountDeleteTarget(lambda grantor: store)
    payload = _effect_payload(incarnation=first.incarnation)
    await target.prepare_once(_binding(), payload)  # STAGE
    with pytest.raises(AccountDisconnectPending, match="account_disconnect_pending"):
        await store.upsert_account(_account(display_name="reconnect"))
    await target.release_once(_binding(), payload)  # ABORT
    again = await store.upsert_account(_account(display_name="reconnect"))
    assert again.incarnation == first.incarnation and await store.get_account("account-1") is not None


@pytest.mark.asyncio
async def test_after_commit_the_hold_is_gone_and_a_reconnect_is_a_new_incarnation():
    store = _store()
    first = await store.upsert_account(_account())
    target = AccountDeleteTarget(lambda grantor: store)
    payload = _effect_payload(incarnation=first.incarnation)
    await target.prepare_once(_binding(), payload)
    await target.apply_once(_binding(), payload)  # COMMIT
    later = await store.upsert_account(_account())
    assert later.incarnation != first.incarnation


@pytest.mark.asyncio
async def test_a_stage_against_a_moved_incarnation_is_refused():
    store = _store()
    await store.upsert_account(_account())
    target = AccountDeleteTarget(lambda grantor: store)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await target.prepare_once(_binding(), _effect_payload(incarnation="0" * 32))


@pytest.mark.asyncio
async def test_a_store_without_the_shared_lock_is_an_unavailable_adapter_not_a_crash():
    store = DelegatedToKdcubeStore(user_id=GRANTOR, backend=_MemoryUserConfiguration())
    target = AccountDeleteTarget(lambda grantor: store)
    for phase in (target.prepare_once, target.apply_once, target.release_once):
        with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
            await phase(_binding(), _effect_payload())

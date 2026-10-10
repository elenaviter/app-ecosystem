"""A store's fallback intent service cannot silently change lock composition."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards import intent_links
from connection_hub.delegated_credentials.cards.card_participant import LocalCardIntentSource
from connection_hub.delegated_credentials.cards.service import CardConflict, DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from test_card_service import ACCESS_ID, SUBJECT_HASH, _Cache


def _lock(calls, name):
    @asynccontextmanager
    async def mutation_lock(**kwargs):
        calls.append((name, kwargs))
        yield

    return mutation_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("same_lock", [False, True])
async def test_second_service_refuses_without_replacing_the_first_fallback(tmp_path, same_lock):
    calls = []
    first_lock = _lock(calls, "first")
    second_lock = first_lock if same_lock else _lock(calls, "second")
    store = BundleStorageDelegatedCardStore(tmp_path)
    first = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=first_lock)
    source = LocalCardIntentSource(store)

    with pytest.raises(CardConflict) as error:
        DelegatedCardService(store=store, cache=_Cache(), mutation_lock=second_lock)
    assert error.value.reason == "card_intent_service_conflict"
    assert store._card_intent_service is first
    assert calls == []  # refusal takes no lock and writes no intent/Card
    assert not source._path("a" * 64).exists()

    async with intent_links.sections(source, [(SUBJECT_HASH, ACCESS_ID)]):
        pass
    assert [name for name, _ in calls] == ["first"]


@pytest.mark.asyncio
async def test_explicit_service_does_not_borrow_the_store_fallback(tmp_path):
    calls = []
    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_lock(calls, "explicit"))
    source = LocalCardIntentSource(store, service=service)

    @asynccontextmanager
    async def wrong_sections(keys):
        raise AssertionError("explicit service must not use the fallback")
        yield  # pragma: no cover

    # A deliberately foreign fallback is an adversarial test input, not a
    # supported service registration. Production passes its service directly.
    store._card_intent_service = SimpleNamespace(_card_version_sections=wrong_sections)
    async with intent_links.sections(source, [(SUBJECT_HASH, ACCESS_ID)]):
        pass
    assert source._service is service
    assert [name for name, _ in calls] == ["explicit"]


def test_distinct_stores_keep_independent_services_and_locks(tmp_path):
    calls = []
    first_store = BundleStorageDelegatedCardStore(tmp_path / "first")
    second_store = BundleStorageDelegatedCardStore(tmp_path / "second")
    first = DelegatedCardService(store=first_store, cache=_Cache(), mutation_lock=_lock(calls, "first"))
    second = DelegatedCardService(store=second_store, cache=_Cache(), mutation_lock=_lock(calls, "second"))
    assert first_store._card_intent_service is first
    assert second_store._card_intent_service is second

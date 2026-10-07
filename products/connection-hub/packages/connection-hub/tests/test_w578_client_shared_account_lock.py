"""W578: every bundle that reaches account records through from_connection_hub shares the app's account lock."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_to_kdcube.account_lock import RedisAccountLock, account_lock_prefix
from connection_hub.delegated_to_kdcube.client import DelegatedToKdcubeClient
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore
from test_account_store import _MemoryUserConfiguration


def _entrypoint(redis):
    return SimpleNamespace(redis=redis, bundle_props={},
                           runtime_identity=lambda: {"tenant": "tenant-a", "project": "project-b"})


async def _props(redis, *, tenant, project, bundle_id):
    return {}


@pytest.mark.asyncio
async def test_a_client_on_redis_binds_the_apps_account_lock_to_every_store_it_builds():
    built = []

    def factory(**kwargs):
        store = DelegatedToKdcubeStore(backend=_MemoryUserConfiguration(), **kwargs)
        built.append(store)
        return store

    client = await DelegatedToKdcubeClient.from_connection_hub(
        _entrypoint(redis=object()), user_id="user-1", connection_hub_bundle_id="connection-hub@1-0",
        store_factory=factory, bundle_props_loader=_props)
    store = client._broker.store
    assert isinstance(store._account_lock, RedisAccountLock)
    assert store._account_lock.prefix == account_lock_prefix("tenant-a", "project-b")
    assert built and built[-1] is store


@pytest.mark.asyncio
async def test_a_given_store_gets_the_lock_and_no_redis_means_no_lock():
    given = DelegatedToKdcubeStore(user_id="user-1", backend=_MemoryUserConfiguration())
    client = await DelegatedToKdcubeClient.from_connection_hub(
        _entrypoint(redis=object()), user_id="user-1", connection_hub_bundle_id="connection-hub@1-0",
        store=given, bundle_props_loader=_props)
    assert isinstance(client._broker.store._account_lock, RedisAccountLock)
    plain = await DelegatedToKdcubeClient.from_connection_hub(
        _entrypoint(redis=None), user_id="user-1", connection_hub_bundle_id="connection-hub@1-0",
        store=DelegatedToKdcubeStore(user_id="user-1", backend=_MemoryUserConfiguration()))
    assert plain._broker.store._account_lock is None

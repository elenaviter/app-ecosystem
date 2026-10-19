"""R-3 policy check without a Redis: CONFIG GET first, INFO memory where a managed Redis refuses CONFIG.

AWS ElastiCache lists CONFIG as a restricted command, so CONFIG GET raises there; INFO memory still reports
``maxmemory_policy``. Every path that does not prove ``noeviction`` refuses.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards import redis_lock
from connection_hub.delegated_credentials.cards.redis_lock import assert_redis_noeviction
from connection_hub.delegated_credentials.cards.store import CardStorageError

LOGGER = redis_lock.__name__


class _Refused(Exception):
    """What a client raises for a command the server will not run."""


class _Redis:
    def __init__(self, *, config=None, info=None):
        self.config, self.info_answer, self.calls = config, info, []

    async def config_get(self, name):
        self.calls.append(("config_get", name))
        if isinstance(self.config, Exception):
            raise self.config
        return self.config

    async def info(self, section):
        self.calls.append(("info", section))
        if isinstance(self.info_answer, Exception):
            raise self.info_answer
        return self.info_answer


def _lines(caplog):
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


@pytest.mark.asyncio
async def test_config_noeviction_is_verified_without_info(caplog):
    redis = _Redis(config={"maxmemory-policy": "noeviction"}, info=AssertionError("INFO must not be read"))
    with caplog.at_level("INFO", logger=LOGGER):
        await assert_redis_noeviction(redis)
    assert redis.calls == [("config_get", "maxmemory-policy")]
    assert _lines(caplog) == ["card lock redis policy noeviction verified via config"]


@pytest.mark.asyncio
async def test_config_evicting_is_refused_without_info(caplog):
    redis = _Redis(config={"maxmemory-policy": b"allkeys-lru"}, info={"maxmemory_policy": "noeviction"})
    with caplog.at_level("INFO", logger=LOGGER):
        with pytest.raises(CardStorageError, match="^card_lock_redis_policy_not_noeviction$"):
            await assert_redis_noeviction(redis)
    assert redis.calls == [("config_get", "maxmemory-policy")]  # a CONFIG answer is never overridden by INFO
    assert _lines(caplog) == ["card lock redis policy refused via config: allkeys-lru"]


@pytest.mark.asyncio
async def test_config_refused_and_info_noeviction_is_verified(caplog):
    redis = _Redis(config=_Refused("unknown command 'CONFIG'"), info={"maxmemory_policy": "noeviction"})
    with caplog.at_level("INFO", logger=LOGGER):
        await assert_redis_noeviction(redis)
    assert redis.calls == [("config_get", "maxmemory-policy"), ("info", "memory")]
    assert _lines(caplog) == ["card lock redis policy noeviction verified via info"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,logged", [("volatile-lru", "volatile-lru"), (b"allkeys-lfu", "allkeys-lfu"),
                                           ("some-future-policy", "other")])
async def test_config_refused_and_info_evicting_is_refused(caplog, policy, logged):
    redis = _Redis(config=_Refused("unknown command 'CONFIG'"), info={"maxmemory_policy": policy})
    with caplog.at_level("INFO", logger=LOGGER):
        with pytest.raises(CardStorageError, match="^card_lock_redis_policy_not_noeviction$"):
            await assert_redis_noeviction(redis)
    assert _lines(caplog) == [f"card lock redis policy refused via info: {logged}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"maxmemory-policy": ""}, None])
async def test_a_config_answer_without_the_policy_is_read_from_info(config):
    redis = _Redis(config=config, info={"maxmemory_policy": "noeviction"})
    await assert_redis_noeviction(redis)
    assert redis.calls[-1] == ("info", "memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [_Refused("connection refused"), {}, {"maxmemory_policy": None}, ["noeviction"]])
async def test_both_failing_is_refused_unverified(caplog, info):
    redis = _Redis(config=_Refused("unknown command 'CONFIG'"), info=info)
    with caplog.at_level("INFO", logger=LOGGER):
        with pytest.raises(CardStorageError, match="^card_lock_redis_policy_unverified$"):
            await assert_redis_noeviction(redis)
    assert _lines(caplog) == ["card lock redis policy unverified: CONFIG GET and INFO memory report none"]
    assert all("connection refused" not in line and "CONFIG'" not in line for line in _lines(caplog))

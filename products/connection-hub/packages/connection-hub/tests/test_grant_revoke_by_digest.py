"""W582 grant_unbind: revoke exactly one pinned binding by its token digest, never by a raw bearer."""

from __future__ import annotations

import hashlib

import pytest

from connection_hub.delegated_credentials.oauth.store import GrantStore


class _Redis:
    def __init__(self):
        self.deleted = []

    async def delete(self, key):
        self.deleted.append(key)
        return 1


class _Authority:
    def __init__(self):
        self.digests = []

    async def revoke_access_grant_by_digest(self, digest):
        self.digests.append(digest)
        return True


TOKEN = "a-bearer-value-never-stored"
DIGEST = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_the_redis_path_deletes_the_same_key_a_raw_token_binding_uses():
    redis = _Redis()
    store = GrantStore(redis, "tenant-a", "project-a")
    assert await store.revoke_access_grant_by_digest(DIGEST) is True
    assert redis.deleted == [store._agrant_key(TOKEN)]


@pytest.mark.asyncio
async def test_the_sql_authority_receives_only_the_pinned_digest():
    authority = _Authority()
    store = GrantStore(object(), "tenant-a", "project-a", authority_store=authority)
    assert await store.revoke_access_grant_by_digest(DIGEST.upper()) is True
    assert authority.digests == [DIGEST]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "short", "z" * 64, TOKEN])
async def test_anything_but_a_digest_is_refused(bad):
    with pytest.raises(ValueError, match="token_sha256_invalid"):
        await GrantStore(_Redis(), "tenant-a", "project-a").revoke_access_grant_by_digest(bad)

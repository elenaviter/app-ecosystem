# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import json

from connection_hub.authority_registry import (
    AuthorityProviderSpec,
    AuthorityRegistry,
    AuthorityResolution,
    CredentialEnvelope,
    RedisAuthorityDiscovery,
    authority_provider_spec_from_declaration,
)


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.operations: list[tuple[str, tuple[object, ...]]] = []

    def info(self, section: str) -> "_Pipeline":
        self.operations.append(("info", (section,)))
        return self

    def mget(self, keys: list[str]) -> "_Pipeline":
        self.operations.append(("mget", (keys,)))
        return self

    def get(self, key: str) -> "_Pipeline":
        self.operations.append(("get", (key,)))
        return self

    def set(self, key: str, value: str) -> "_Pipeline":
        self.operations.append(("set", (key, value)))
        return self

    def setex(self, key: str, ttl: int, value: str) -> "_Pipeline":
        self.operations.append(("set", (key, value)))
        assert ttl > 0
        return self

    def sadd(self, key: str, value: str) -> "_Pipeline":
        self.operations.append(("sadd", (key, value)))
        return self

    def expire(self, key: str, ttl: int) -> "_Pipeline":
        self.operations.append(("expire", (key, ttl)))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for operation, arguments in self.operations:
            if operation == "info":
                results.append({"run_id": self.redis.run_id})
            elif operation == "mget":
                [keys] = arguments
                results.append([self.redis.values.get(key) for key in keys])
            elif operation == "get":
                [key] = arguments
                results.append(self.redis.values.get(str(key)))
            elif operation == "set":
                key, value = arguments
                self.redis.values[str(key)] = str(value)
                results.append(True)
            elif operation == "sadd":
                key, value = arguments
                self.redis.sets.setdefault(str(key), set()).add(str(value))
                results.append(1)
            else:
                results.append(True)
        return results


class _Redis:
    def __init__(self) -> None:
        self.run_id = "run-a"
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.eval_calls = 0
        self.last_eval_keys: list[str] = []

    async def info(self, section: str):
        assert section == "server"
        return {"run_id": self.run_id}

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        if nx and key in self.values:
            return False
        if ex is not None:
            assert ex > 0
        self.values[key] = value
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)

    async def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, value: str):
        existed = value in self.sets.get(key, set())
        self.sets.setdefault(key, set()).discard(value)
        return int(existed)

    async def delete(self, key: str):
        existed = key in self.values or key in self.sets
        self.values.pop(key, None)
        self.sets.pop(key, None)
        return int(existed)

    async def eval(self, script: str, numkeys: int, *arguments: str):
        self.eval_calls += 1
        self.last_eval_keys = list(arguments[:numkeys])
        if numkeys == 1:
            key, token = arguments
            if self.values.get(key) == token:
                self.values.pop(key, None)
                return 1
            return 0
        if "allowed_keys" in script:
            keys = list(arguments[:numkeys])
            argv = list(arguments[numkeys:])
            all_key, bundle_key, publication_key, *_record_keys = keys
            bundle_id, _ttl, raw_count, *publication_arguments = argv
            count = int(raw_count)
            allowed = set(keys[3:])
            if not self.sets.get(bundle_key, set()).issubset(allowed):
                return -1
            for record_key in list(self.sets.get(bundle_key, set())):
                raw = self.values.get(record_key)
                record = json.loads(raw) if raw else {}
                if record.get("bundle_id") == bundle_id:
                    self.values.pop(record_key, None)
                    self.sets.setdefault(all_key, set()).discard(record_key)
            self.sets.pop(bundle_key, None)
            for index in range(count):
                argument_index = index * 2
                key_index = int(publication_arguments[argument_index]) - 1
                raw = publication_arguments[argument_index + 1]
                record_key = keys[key_index]
                self.values[record_key] = raw
                self.sets.setdefault(all_key, set()).add(record_key)
                self.sets.setdefault(bundle_key, set()).add(record_key)
            self.values[publication_key] = publication_arguments[count * 2]
            return count
        assert numkeys == 2
        all_key, record_key, member, observed = arguments
        current = self.values.get(record_key)
        if current is None or current == observed:
            self.sets.setdefault(all_key, set()).discard(member)
            self.values.pop(record_key, None)
            return 1
        return 0


class _Provider:
    spec = AuthorityProviderSpec(
        authority_id="example.authority",
        credential_kinds=("authority_access",),
        audiences=("bundle:example",),
        authenticators=("example.oauth",),
    )

    async def verify_credential(self, credential, *, token="", context=None):
        envelope = CredentialEnvelope.coerce(credential)
        return AuthorityResolution(
            ok=True,
            authority_id=envelope.issuer_authority_id,
            authenticator_id=envelope.issuer_authenticator_id,
            subject=envelope.subject,
            credential=envelope,
            metadata={"token_present": bool(token), "context": dict(context or {})},
        )


async def test_local_authority_registry_routes_by_credential_envelope():
    registry = AuthorityRegistry()
    registry.register(_Provider())

    credential = CredentialEnvelope(
        credential_id="cred_test",
        credential_kind="authority_access",
        issuer_authority_id="example.authority",
        issuer_authenticator_id="example.oauth",
        subject="example:user:123",
        audience="bundle:example",
    )

    result = await registry.verify(credential, token="bearer", context={"surface": "mcp"})

    assert result.ok is True
    assert result.authority_id == "example.authority"
    assert result.authenticator_id == "example.oauth"
    assert result.subject == "example:user:123"
    assert result.metadata["token_present"] is True
    assert result.metadata["context"]["surface"] == "mcp"


async def test_local_authority_registry_fails_closed_when_unreachable():
    registry = AuthorityRegistry()

    result = await registry.verify({
        "schema": "kdcube.credential.v1",
        "issuer_authority_id": "bundle.local.only",
        "issuer_authenticator_id": "bundle.local.only.oauth",
        "credential_kind": "authority_access",
        "subject": "bundle:user:1",
    })

    assert result.ok is False
    assert result.error == "authority_not_registered"


def test_authority_provider_spec_from_bundle_declaration():
    spec = authority_provider_spec_from_declaration(
        {
            "authority_id": "custom.identity",
            "authenticator_id": "custom.identity.oauth",
            "credential_kinds": ["authority_access"],
            "audiences": ["bundle:custom-app@1-0"],
            "label": "Custom Identity",
            "transports": ["local"],
        },
        bundle_id="custom-app@1-0",
    )

    assert spec.authority_id == "custom.identity"
    assert spec.authenticators == ("custom.identity.oauth",)
    assert spec.credential_kinds == ("authority_access",)
    assert spec.bundle_id == "custom-app@1-0"
    assert spec.metadata["source"] == "bundle_manifest"


async def test_redis_authority_discovery_rejects_a_restored_prior_run():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    await discovery.register_provider(
        AuthorityProviderSpec(
            authority_id="custom.identity",
            bundle_id="custom@1-0",
        )
    )

    assert [spec.authority_id for spec in await discovery.list_providers()] == [
        "custom.identity"
    ]

    redis.run_id = "run-after-restore"

    assert await discovery.list_providers() == []


async def test_bundle_authority_generation_replaces_removed_declarations_atomically():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    first = AuthorityProviderSpec(
        authority_id="custom.first",
        bundle_id="custom@1-0",
    )
    second = AuthorityProviderSpec(
        authority_id="custom.second",
        bundle_id="custom@1-0",
    )
    records = await discovery.replace_bundle_providers(
        "custom@1-0",
        [first, second],
    )
    assert len({record["generation"] for record in records}) == 1

    await discovery.replace_bundle_providers("custom@1-0", [second])

    assert [spec.authority_id for spec in await discovery.list_providers()] == [
        "custom.second"
    ]

    await discovery.replace_bundle_providers("custom@1-0", [])

    assert await discovery.list_providers() == []


async def test_bundle_authority_publication_is_digest_idempotent_within_one_run():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    spec = AuthorityProviderSpec(
        authority_id="custom.identity",
        bundle_id="custom@1-0",
    )

    assert await discovery.ensure_bundle_providers("custom@1-0", [spec]) is True
    first_eval_count = redis.eval_calls
    assert await discovery.ensure_bundle_providers("custom@1-0", [spec]) is False
    assert redis.eval_calls == first_eval_count

    redis.run_id = "run-after-restart"

    assert await discovery.ensure_bundle_providers("custom@1-0", [spec]) is True
    assert redis.eval_calls == first_eval_count + 1
    assert [item.authority_id for item in await discovery.list_providers()] == [
        "custom.identity"
    ]


async def test_bundle_replace_passes_every_record_key_through_lua_keys():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    spec = AuthorityProviderSpec(
        authority_id="custom.identity",
        bundle_id="custom@1-0",
    )

    await discovery.replace_bundle_providers("custom@1-0", [spec])

    assert discovery._authority_key(spec.authority_id) in redis.last_eval_keys
    assert len({key.split("{")[1].split("}")[0] for key in redis.last_eval_keys}) == 1


async def test_discovery_epoch_is_fenced_by_redis_run():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")

    assert await discovery.epoch_is_current() is False
    await discovery.mark_epoch_current(expected_run_id="run-a")
    assert await discovery.epoch_is_current() is True

    redis.run_id = "run-after-restart"

    assert await discovery.epoch_is_current() is False


async def test_discovery_removes_legacy_key_part_records():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    authority_id = "custom/id"
    redis.sets[discovery._legacy_all_key()] = {authority_id}
    redis.values[discovery._legacy_authority_key(authority_id)] = "{}"

    result = await discovery.reconcile_source({})

    assert result.changed is True
    assert discovery._legacy_all_key() not in redis.sets or not redis.sets[
        discovery._legacy_all_key()
    ]
    assert discovery._legacy_authority_key(authority_id) not in redis.values


async def test_source_reconcile_writes_only_when_authority_source_changes():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    first = AuthorityProviderSpec(
        authority_id="custom.identity",
        bundle_id="custom@1-0",
        label="First",
    )

    initial = await discovery.reconcile_source({"custom@1-0": [first]})
    first_eval_count = redis.eval_calls
    unchanged = await discovery.reconcile_source({"custom@1-0": [first]})
    updated = await discovery.reconcile_source(
        {
            "custom@1-0": [
                AuthorityProviderSpec(
                    authority_id="custom.identity",
                    bundle_id="custom@1-0",
                    label="Updated",
                )
            ]
        }
    )

    assert initial.changed is True
    assert unchanged.changed is False
    assert redis.eval_calls == first_eval_count + 1
    assert updated.changed is True


async def test_complete_reader_recovers_once_from_durable_source_after_restart():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    spec = AuthorityProviderSpec(
        authority_id="custom.identity",
        bundle_id="custom@1-0",
    )
    await discovery.reconcile_source({"custom@1-0": [spec]})
    redis.run_id = "run-after-restart"
    read_through_calls = 0

    async def _read_through():
        nonlocal read_through_calls
        read_through_calls += 1
        result = await discovery.reconcile_source(
            {"custom@1-0": [spec]},
            force=True,
        )
        return result.providers

    recovered = await discovery.list_providers(read_through=_read_through)
    cached = await discovery.list_providers(read_through=_read_through)

    assert [item.authority_id for item in recovered] == ["custom.identity"]
    assert [item.authority_id for item in cached] == ["custom.identity"]
    assert read_through_calls == 1


async def test_complete_reader_rejects_records_that_do_not_match_source_digest():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    spec = AuthorityProviderSpec(
        authority_id="custom.identity",
        bundle_id="custom@1-0",
        label="Durable label",
    )
    await discovery.reconcile_source({"custom@1-0": [spec]})
    record_key = discovery._authority_key(spec.authority_id)
    record = json.loads(redis.values[record_key])
    record["spec"]["label"] = "Mixed generation"
    redis.values[record_key] = json.dumps(record)
    read_through_calls = 0

    async def _read_through():
        nonlocal read_through_calls
        read_through_calls += 1
        return [spec]

    recovered = await discovery.list_providers(read_through=_read_through)

    assert recovered == [spec]
    assert read_through_calls == 1


async def test_complete_empty_generation_does_not_issue_an_empty_mget():
    redis = _Redis()
    discovery = RedisAuthorityDiscovery(redis, tenant="tenant", project="project")
    await discovery.reconcile_source({})

    async def _unexpected_read_through():
        raise AssertionError("a valid empty generation must not read through")

    assert await discovery.list_providers(
        read_through=_unexpected_read_through,
    ) == []

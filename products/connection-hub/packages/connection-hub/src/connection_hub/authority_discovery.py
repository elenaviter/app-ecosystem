# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis projection of authority-provider metadata from durable manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from connection_hub.authority_registry import AuthorityProviderSpec

AUTHORITY_DISCOVERY_SCHEMA = "kdcube.authority.discovery.v2"
AUTHORITY_DISCOVERY_PUBLICATION_SCHEMA = "kdcube.authority.discovery.publication.v1"
AUTHORITY_DISCOVERY_EPOCH_SCHEMA = "kdcube.authority.discovery.epoch.v1"
AUTHORITY_DISCOVERY_RECONCILE_LOCK_TTL_SECONDS = 300
AUTHORITY_DISCOVERY_RECONCILE_LOCK_WAIT_SECONDS = 30.0

_REPLACE_BUNDLE_PROVIDERS_LUA = """
local allowed_keys = {}
for index = 4, #KEYS do
  allowed_keys[KEYS[index]] = index
end
local old_keys = redis.call('SMEMBERS', KEYS[2])
for _, record_key in ipairs(old_keys) do
  if not allowed_keys[record_key] then
    return -1
  end
end
for _, record_key in ipairs(old_keys) do
  local raw = redis.call('GET', record_key)
  if raw then
    local ok, record = pcall(cjson.decode, raw)
    if ok and type(record) == 'table' and record['bundle_id'] == ARGV[1] then
      redis.call('DEL', record_key)
      redis.call('SREM', KEYS[1], record_key)
    end
  else
    redis.call('SREM', KEYS[1], record_key)
  end
end
redis.call('DEL', KEYS[2])
local ttl = tonumber(ARGV[2])
local new_count = tonumber(ARGV[3]) or 0
for offset = 0, new_count - 1 do
  local argument_index = 4 + (offset * 2)
  local key_index = tonumber(ARGV[argument_index])
  local record_key = KEYS[key_index]
  local record = ARGV[argument_index + 1]
  if ttl and ttl > 0 then
    redis.call('SET', record_key, record, 'EX', ttl)
  else
    redis.call('SET', record_key, record)
  end
  redis.call('SADD', KEYS[1], record_key)
  redis.call('SADD', KEYS[2], record_key)
end
local publication = ARGV[4 + (new_count * 2)]
if ttl and ttl > 0 then
  redis.call('SET', KEYS[3], publication, 'EX', ttl)
elseif publication then
  redis.call('SET', KEYS[3], publication)
end
if ttl and ttl > 0 and new_count > 0 then
  redis.call('EXPIRE', KEYS[2], ttl)
end
return new_count
"""

_RELEASE_RECONCILE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class AuthorityDiscoveryRunChangedError(RuntimeError):
    """Redis restarted while a complete discovery generation was rebuilt."""


class AuthorityDiscoveryCoordinationError(RuntimeError):
    """A complete source reconciliation could not acquire its scope lease."""


@dataclass(frozen=True)
class AuthorityDiscoveryReconcileResult:
    """One source-owned projection reconciliation."""

    changed: bool
    run_id: str
    source_digest: str
    providers: tuple["AuthorityProviderSpec", ...]
    bundle_count: int
    changed_bundle_count: int


def _str(value: Any) -> str:
    return str(value or "").strip()


def _key_part(value: Any) -> str:
    text = _str(value)
    return (
        "".join(
            ch if ch.isalnum() or ch in {"-", "_", ".", "@", ":"} else "_"
            for ch in text
        )
        or "_"
    )


class RedisAuthorityDiscovery:
    """Redis-backed authority spec discovery table for one tenant/project."""

    def __init__(self, redis: Any, *, tenant: str, project: str, ttl_seconds: int = 0) -> None:
        self.redis = redis
        self.tenant = _str(tenant)
        self.project = _str(project)
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self._legacy_base = (
            f"kdcube:authorities:{_key_part(self.tenant)}:{_key_part(self.project)}"
        )
        scope = hashlib.sha256(
            f"{self.tenant}\0{self.project}".encode("utf-8")
        ).hexdigest()
        # Every key participating in one transaction/script shares a Redis
        # Cluster slot while the readable suffix still identifies the scope.
        self._base = (
            f"kdcube:authorities:{{{scope}}}:"
            f"{_key_part(self.tenant)}:{_key_part(self.project)}"
        )

    def _authority_key(self, authority_id: str) -> str:
        authority = _str(authority_id)
        digest = hashlib.sha256(authority.encode("utf-8")).hexdigest()
        return f"{self._base}:authority:{digest}"

    def _all_key(self) -> str:
        return f"{self._base}:authorities"

    def _bundle_key(self, bundle_id: str) -> str:
        digest = hashlib.sha256(_str(bundle_id).encode("utf-8")).hexdigest()
        return f"{self._base}:bundle:{digest}:authorities"

    def _bundle_publication_key(self, bundle_id: str) -> str:
        digest = hashlib.sha256(_str(bundle_id).encode("utf-8")).hexdigest()
        return f"{self._base}:bundle:{digest}:publication"

    def _epoch_key(self) -> str:
        return f"{self._base}:epoch"

    def _reconcile_lock_key(self) -> str:
        return f"{self._base}:reconcile-lock"

    def _legacy_all_key(self) -> str:
        return f"{self._legacy_base}:authorities"

    def _legacy_authority_key(self, authority_id: str) -> str:
        return f"{self._legacy_base}:authority:{_key_part(authority_id)}"

    async def current_run_id(self) -> str:
        info = await self.redis.info("server")
        run_id = _str((info or {}).get("run_id"))
        if not run_id:
            raise RuntimeError("authority discovery Redis run id is unavailable")
        return run_id

    async def _run_id(self) -> str:
        return await self.current_run_id()

    @asynccontextmanager
    async def source_reconciliation_lock(self) -> AsyncIterator[None]:
        """Serialize durable-source reads and complete projection commits."""

        key = self._reconcile_lock_key()
        token = secrets.token_hex(24)
        deadline = time.monotonic() + AUTHORITY_DISCOVERY_RECONCILE_LOCK_WAIT_SECONDS
        while True:
            try:
                acquired = await self.redis.set(
                    key,
                    token,
                    nx=True,
                    ex=AUTHORITY_DISCOVERY_RECONCILE_LOCK_TTL_SECONDS,
                )
            except Exception as exc:
                raise AuthorityDiscoveryCoordinationError(
                    "authority discovery reconciliation coordination is unavailable"
                ) from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise AuthorityDiscoveryCoordinationError(
                    "timed out waiting for authority discovery reconciliation"
                )
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            try:
                await self.redis.eval(
                    _RELEASE_RECONCILE_LOCK_LUA,
                    1,
                    key,
                    token,
                )
            except Exception:
                # The lease expires. Never delete a lock now owned by another worker.
                pass

    @staticmethod
    def _decode_mapping(raw: Any) -> dict[str, Any]:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            value = json.loads(raw) if raw is not None else {}
        except Exception:
            value = {}
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _normalize_bundle_specs(
        bundle: str,
        specs: list[AuthorityProviderSpec | Mapping[str, Any]]
        | tuple[AuthorityProviderSpec | Mapping[str, Any], ...],
    ) -> list[AuthorityProviderSpec]:
        normalized: list[AuthorityProviderSpec] = []
        authority_ids: set[str] = set()
        for value in specs:
            spec = (
                value
                if isinstance(value, AuthorityProviderSpec)
                else AuthorityProviderSpec.from_dict(value)
            )
            if not spec.authority_id:
                raise ValueError("authority_id is required")
            if spec.bundle_id != bundle:
                raise ValueError("authority provider bundle_id does not match publication")
            if spec.authority_id in authority_ids:
                raise ValueError("authority provider is duplicated in bundle publication")
            authority_ids.add(spec.authority_id)
            normalized.append(spec)
        normalized.sort(key=lambda item: item.authority_id)
        return normalized

    @staticmethod
    def _manifest_digest(bundle: str, specs: list[AuthorityProviderSpec]) -> str:
        payload = {
            "bundle_id": bundle,
            "providers": [spec.to_dict() for spec in specs],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _record(
        provider_spec: AuthorityProviderSpec,
        *,
        run_id: str,
        generation: str,
        now: float,
        ttl: int,
    ) -> dict[str, Any]:
        return {
            "schema": AUTHORITY_DISCOVERY_SCHEMA,
            "spec": provider_spec.to_dict(),
            "bundle_id": provider_spec.bundle_id,
            "generation": generation,
            "redis_run_id": run_id,
            "registered_at": now,
            "expires_at": now + ttl if ttl > 0 else 0,
        }

    async def register_provider(
        self,
        spec: AuthorityProviderSpec | Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        provider_spec = (
            spec
            if isinstance(spec, AuthorityProviderSpec)
            else AuthorityProviderSpec.from_dict(spec)
        )
        if not provider_spec.authority_id:
            raise ValueError("authority_id is required")
        ttl = max(
            0,
            int(self.ttl_seconds if ttl_seconds is None else ttl_seconds),
        )
        record = self._record(
            provider_spec,
            run_id=await self._run_id(),
            generation=secrets.token_hex(16),
            now=time.time(),
            ttl=ttl,
        )
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = self._authority_key(provider_spec.authority_id)
        pipeline = self.redis.pipeline(transaction=True)
        if ttl > 0:
            pipeline.setex(key, ttl, raw)
        else:
            pipeline.set(key, raw)
        pipeline.sadd(self._all_key(), key)
        if provider_spec.bundle_id:
            bundle_key = self._bundle_key(provider_spec.bundle_id)
            pipeline.sadd(bundle_key, key)
            if ttl > 0:
                pipeline.expire(bundle_key, ttl)
        await pipeline.execute()
        return record

    async def replace_bundle_providers(
        self,
        bundle_id: str,
        specs: list[AuthorityProviderSpec | Mapping[str, Any]]
        | tuple[AuthorityProviderSpec | Mapping[str, Any], ...],
        *,
        ttl_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically publish the complete authority generation for a bundle."""

        bundle = _str(bundle_id)
        if not bundle:
            raise ValueError("bundle_id is required")
        normalized = self._normalize_bundle_specs(bundle, specs)
        ttl = max(
            0,
            int(self.ttl_seconds if ttl_seconds is None else ttl_seconds),
        )
        run_id = await self._run_id()
        return await self._replace_normalized_bundle_providers(
            bundle=bundle,
            specs=normalized,
            ttl=ttl,
            run_id=run_id,
            manifest_digest=self._manifest_digest(bundle, normalized),
        )

    async def ensure_bundle_providers(
        self,
        bundle_id: str,
        specs: list[AuthorityProviderSpec | Mapping[str, Any]]
        | tuple[AuthorityProviderSpec | Mapping[str, Any], ...],
        *,
        ttl_seconds: int | None = None,
        expected_run_id: str = "",
    ) -> bool:
        """Publish a bundle only when its manifest or Redis run changed."""

        bundle = _str(bundle_id)
        if not bundle:
            raise ValueError("bundle_id is required")
        normalized = self._normalize_bundle_specs(bundle, specs)
        digest = self._manifest_digest(bundle, normalized)
        ttl = max(
            0,
            int(self.ttl_seconds if ttl_seconds is None else ttl_seconds),
        )
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.get(self._bundle_publication_key(bundle))
        info, raw_publication = await pipeline.execute()
        run_id = _str((info or {}).get("run_id"))
        if not run_id:
            raise RuntimeError("authority discovery Redis run id is unavailable")
        expected = _str(expected_run_id)
        if expected and run_id != expected:
            raise AuthorityDiscoveryRunChangedError(
                "authority discovery Redis run changed before publication"
            )
        publication = self._decode_mapping(raw_publication)
        if (
            publication.get("schema") == AUTHORITY_DISCOVERY_PUBLICATION_SCHEMA
            and _str(publication.get("bundle_id")) == bundle
            and _str(publication.get("redis_run_id")) == run_id
            and _str(publication.get("manifest_digest")) == digest
        ):
            return False
        await self._replace_normalized_bundle_providers(
            bundle=bundle,
            specs=normalized,
            ttl=ttl,
            run_id=run_id,
            manifest_digest=digest,
        )
        return True

    async def _replace_normalized_bundle_providers(
        self,
        *,
        bundle: str,
        specs: list[AuthorityProviderSpec],
        ttl: int,
        run_id: str,
        manifest_digest: str,
    ) -> list[dict[str, Any]]:
        generation = secrets.token_hex(16)
        now = time.time()
        records = [
            self._record(
                spec,
                run_id=run_id,
                generation=generation,
                now=now,
                ttl=ttl,
            )
            for spec in specs
        ]
        publication = json.dumps(
            {
                "schema": AUTHORITY_DISCOVERY_PUBLICATION_SCHEMA,
                "bundle_id": bundle,
                "manifest_digest": manifest_digest,
                "generation": generation,
                "redis_run_id": run_id,
                "provider_count": len(records),
                "published_at": now,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        bundle_key = self._bundle_key(bundle)
        new_keys = [self._authority_key(spec.authority_id) for spec in specs]
        for _attempt in range(3):
            raw_old_keys = await self.redis.smembers(bundle_key)
            old_keys = [
                value.decode("utf-8")
                if isinstance(value, (bytes, bytearray))
                else str(value)
                for value in (raw_old_keys or [])
            ]
            record_keys = sorted(set(old_keys).union(new_keys))
            keys = [
                self._all_key(),
                bundle_key,
                self._bundle_publication_key(bundle),
                *record_keys,
            ]
            key_positions = {key: index + 4 for index, key in enumerate(record_keys)}
            arguments: list[str] = [bundle, str(ttl), str(len(records))]
            for key, record in zip(new_keys, records):
                arguments.extend(
                    [
                        str(key_positions[key]),
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                )
            arguments.append(publication)
            result = await self.redis.eval(
                _REPLACE_BUNDLE_PROVIDERS_LUA,
                len(keys),
                *keys,
                *arguments,
            )
            if int(result or 0) >= 0:
                return records
        raise RuntimeError("authority discovery bundle generation changed during publication")

    @classmethod
    def _normalize_source(
        cls,
        bundle_specs: Mapping[
            str,
            Sequence[AuthorityProviderSpec | Mapping[str, Any]],
        ],
    ) -> dict[str, list[AuthorityProviderSpec]]:
        normalized: dict[str, list[AuthorityProviderSpec]] = {}
        owners: dict[str, str] = {}
        for raw_bundle, raw_specs in bundle_specs.items():
            bundle = _str(raw_bundle)
            if not bundle:
                raise ValueError("bundle_id is required")
            if bundle in normalized:
                raise ValueError("bundle_id is duplicated in authority discovery source")
            specs = cls._normalize_bundle_specs(bundle, list(raw_specs))
            if not specs:
                continue
            for spec in specs:
                existing = owners.get(spec.authority_id)
                if existing is not None and existing != bundle:
                    raise ValueError(
                        f"authority provider '{spec.authority_id}' is declared by both "
                        f"'{existing}' and '{bundle}'"
                    )
                owners[spec.authority_id] = bundle
            normalized[bundle] = specs
        return dict(sorted(normalized.items()))

    @staticmethod
    def _source_digest(
        bundle_specs: Mapping[str, Sequence[AuthorityProviderSpec]],
    ) -> str:
        encoded = json.dumps(
            {
                bundle: [spec.to_dict() for spec in specs]
                for bundle, specs in sorted(bundle_specs.items())
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _flatten_source(
        bundle_specs: Mapping[str, Sequence[AuthorityProviderSpec]],
    ) -> tuple[AuthorityProviderSpec, ...]:
        return tuple(
            sorted(
                (spec for specs in bundle_specs.values() for spec in specs),
                key=lambda spec: spec.authority_id,
            )
        )

    @staticmethod
    def _ids_digest(authority_ids: Sequence[str]) -> str:
        encoded = json.dumps(
            sorted(_str(value) for value in authority_ids if _str(value)),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _read_epoch(self) -> tuple[str, dict[str, Any]]:
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.get(self._epoch_key())
        info, raw_epoch = await pipeline.execute()
        run_id = _str((info or {}).get("run_id"))
        return run_id, self._decode_mapping(raw_epoch)

    async def epoch_is_current(self, *, source_digest: str = "") -> bool:
        """Return whether a complete source generation covers this Redis run."""

        run_id, epoch = await self._read_epoch()
        expected_digest = _str(source_digest)
        return bool(
            run_id
            and epoch.get("schema") == AUTHORITY_DISCOVERY_EPOCH_SCHEMA
            and _str(epoch.get("redis_run_id")) == run_id
            and (
                not expected_digest
                or _str(epoch.get("source_digest")) == expected_digest
            )
        )

    async def invalidate_epoch(self) -> None:
        await self.redis.delete(self._epoch_key())

    async def mark_epoch_current(
        self,
        *,
        expected_run_id: str,
        source_digest: str = "",
        source_revision: int = 0,
        bundle_ids: Sequence[str] = (),
        authority_ids: Sequence[str] = (),
    ) -> None:
        """Mark a full sweep complete only for the run it rebuilt."""

        expected = _str(expected_run_id)
        if not expected:
            raise ValueError("expected_run_id is required")
        normalized_bundle_ids = sorted(
            {_str(value) for value in bundle_ids if _str(value)}
        )
        normalized_authority_ids = sorted(
            {_str(value) for value in authority_ids if _str(value)}
        )
        raw = json.dumps(
            {
                "schema": AUTHORITY_DISCOVERY_EPOCH_SCHEMA,
                "redis_run_id": expected,
                "source_digest": _str(source_digest),
                "source_revision": max(0, int(source_revision or 0)),
                "bundle_ids": normalized_bundle_ids,
                "authority_ids": normalized_authority_ids,
                "authority_ids_digest": self._ids_digest(normalized_authority_ids),
                "provider_count": len(normalized_authority_ids),
                "published_at": time.time(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.set(self._epoch_key(), raw)
        info, _installed = await pipeline.execute()
        if _str((info or {}).get("run_id")) != expected:
            raise AuthorityDiscoveryRunChangedError(
                "authority discovery Redis run changed before epoch commit"
            )

    async def reconcile_source(
        self,
        bundle_specs: Mapping[
            str,
            Sequence[AuthorityProviderSpec | Mapping[str, Any]],
        ],
        *,
        source_revision: int = 0,
        force: bool = False,
    ) -> AuthorityDiscoveryReconcileResult:
        """Project one durable source generation into Redis.

        Callers invoke this at the source-change boundary. A reader may invoke
        it with ``force=True`` only after it proved the projection missing or
        invalid; that is read-through recovery, not request-time maintenance.
        """

        normalized = self._normalize_source(bundle_specs)
        providers = self._flatten_source(normalized)
        source_digest = self._source_digest(normalized)
        run_id, previous_epoch = await self._read_epoch()
        if not run_id:
            raise RuntimeError("authority discovery Redis run id is unavailable")
        authority_ids = [spec.authority_id for spec in providers]
        bundle_ids = list(normalized)
        previous_bundle_ids = {
            _str(value)
            for value in previous_epoch.get("bundle_ids") or ()
            if _str(value)
        }
        current_epoch = bool(
            previous_epoch.get("schema") == AUTHORITY_DISCOVERY_EPOCH_SCHEMA
            and _str(previous_epoch.get("redis_run_id")) == run_id
            and _str(previous_epoch.get("source_digest")) == source_digest
            and int(previous_epoch.get("provider_count") or 0) == len(providers)
            and sorted(previous_epoch.get("authority_ids") or ()) == authority_ids
            and sorted(previous_epoch.get("bundle_ids") or ()) == bundle_ids
        )
        if current_epoch and not force:
            return AuthorityDiscoveryReconcileResult(
                changed=False,
                run_id=run_id,
                source_digest=source_digest,
                providers=providers,
                bundle_count=len(normalized),
                changed_bundle_count=0,
            )

        await self.invalidate_epoch()
        changed_bundle_count = 0
        for bundle, specs in normalized.items():
            if force:
                await self._replace_normalized_bundle_providers(
                    bundle=bundle,
                    specs=specs,
                    ttl=self.ttl_seconds,
                    run_id=run_id,
                    manifest_digest=self._manifest_digest(bundle, specs),
                )
                changed = True
            else:
                changed = await self.ensure_bundle_providers(
                    bundle,
                    specs,
                    expected_run_id=run_id,
                )
            changed_bundle_count += int(changed)

        for removed_bundle in sorted(previous_bundle_ids.difference(normalized)):
            if force:
                await self._replace_normalized_bundle_providers(
                    bundle=removed_bundle,
                    specs=[],
                    ttl=self.ttl_seconds,
                    run_id=run_id,
                    manifest_digest=self._manifest_digest(removed_bundle, []),
                )
                changed = True
            else:
                changed = await self.ensure_bundle_providers(
                    removed_bundle,
                    [],
                    expected_run_id=run_id,
                )
            changed_bundle_count += int(changed)

        await self._purge_legacy_records()
        await self.mark_epoch_current(
            expected_run_id=run_id,
            source_digest=source_digest,
            source_revision=source_revision,
            bundle_ids=bundle_ids,
            authority_ids=authority_ids,
        )
        return AuthorityDiscoveryReconcileResult(
            changed=True,
            run_id=run_id,
            source_digest=source_digest,
            providers=providers,
            bundle_count=len(normalized),
            changed_bundle_count=changed_bundle_count,
        )

    async def reconcile_from_source(
        self,
        source_loader: Callable[
            [],
            Awaitable[
                Mapping[
                    str,
                    Sequence[AuthorityProviderSpec | Mapping[str, Any]],
                ]
            ],
        ],
        *,
        source_revision: int = 0,
        force: bool = False,
    ) -> AuthorityDiscoveryReconcileResult:
        """Load durable authority state and project it under one scope lease."""

        async with self.source_reconciliation_lock():
            return await self.reconcile_source(
                await source_loader(),
                source_revision=source_revision,
                force=force,
            )

    async def _purge_legacy_records(self) -> None:
        """Remove v1 key-part records after discovery moved to full-id hashes."""

        try:
            raw_members = await self.redis.smembers(self._legacy_all_key())
        except Exception:
            return
        for raw_member in raw_members or []:
            member = (
                raw_member.decode("utf-8")
                if isinstance(raw_member, (bytes, bytearray))
                else str(raw_member)
            )
            try:
                await self.redis.delete(self._legacy_authority_key(member))
                await self.redis.srem(self._legacy_all_key(), member)
            except Exception:
                continue

    async def _list_complete_projection(self) -> list[AuthorityProviderSpec] | None:
        """Read and validate one complete projection without repairing it."""

        raw_members = await self.redis.smembers(self._all_key())
        members = sorted(
            item.decode("utf-8") if isinstance(item, (bytes, bytearray)) else str(item)
            for item in (raw_members or [])
        )
        record_prefix = f"{self._base}:authority:"
        if any(not member.startswith(record_prefix) for member in members):
            return None
        keys = members
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.get(self._epoch_key())
        if keys:
            pipeline.mget(keys)
        results = await pipeline.execute()
        info, raw_epoch = results[:2]
        raw_records = results[2] if keys else []
        run_id = _str((info or {}).get("run_id"))
        if not run_id:
            return None
        epoch = self._decode_mapping(raw_epoch)
        authority_ids = sorted(
            _str(value)
            for value in epoch.get("authority_ids") or ()
            if _str(value)
        )
        expected_keys = sorted(self._authority_key(value) for value in authority_ids)
        valid_epoch = bool(
            epoch.get("schema") == AUTHORITY_DISCOVERY_EPOCH_SCHEMA
            and _str(epoch.get("redis_run_id")) == run_id
            and bool(_str(epoch.get("source_digest")))
            and int(epoch.get("provider_count") or 0) == len(authority_ids)
            and _str(epoch.get("authority_ids_digest")) == self._ids_digest(authority_ids)
            and members == expected_keys
            and len(raw_records or ()) == len(expected_keys)
        )
        if not valid_epoch:
            return None
        specs_by_id: dict[str, AuthorityProviderSpec] = {}
        for key, raw in zip(keys, raw_records or []):
            if raw is None:
                return None
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
            if not isinstance(data, Mapping):
                data = {}
            spec = AuthorityProviderSpec.from_dict(data.get("spec") or {})
            valid = (
                data.get("schema") == AUTHORITY_DISCOVERY_SCHEMA
                and _str(data.get("redis_run_id")) == run_id
                and bool(spec.authority_id)
                and self._authority_key(spec.authority_id) == key
            )
            if valid:
                specs_by_id[spec.authority_id] = spec
                continue
            return None
        if sorted(specs_by_id) != authority_ids:
            return None
        observed_source: dict[str, list[AuthorityProviderSpec]] = {}
        for spec in specs_by_id.values():
            bundle_id = _str(spec.bundle_id)
            if not bundle_id:
                return None
            observed_source.setdefault(bundle_id, []).append(spec)
        for specs in observed_source.values():
            specs.sort(key=lambda item: item.authority_id)
        epoch_bundle_ids = sorted(
            _str(value)
            for value in epoch.get("bundle_ids") or ()
            if _str(value)
        )
        if sorted(observed_source) != epoch_bundle_ids:
            return None
        if self._source_digest(observed_source) != _str(epoch.get("source_digest")):
            return None
        return [specs_by_id[key] for key in authority_ids]

    async def _list_run_fenced_records(self) -> list[AuthorityProviderSpec]:
        """Compatibility read for callers that do not require a source epoch."""

        raw_members = await self.redis.smembers(self._all_key())
        members = sorted(
            item.decode("utf-8") if isinstance(item, (bytes, bytearray)) else str(item)
            for item in (raw_members or [])
        )
        if not members:
            return []
        record_prefix = f"{self._base}:authority:"
        keys = [
            member if member.startswith(record_prefix) else self._authority_key(member)
            for member in members
        ]
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.info("server")
        pipeline.mget(keys)
        info, raw_records = await pipeline.execute()
        run_id = _str((info or {}).get("run_id"))
        if not run_id:
            return []
        specs_by_id: dict[str, AuthorityProviderSpec] = {}
        for key, raw in zip(keys, raw_records or []):
            data = self._decode_mapping(raw)
            spec = AuthorityProviderSpec.from_dict(data.get("spec") or {})
            if (
                data.get("schema") == AUTHORITY_DISCOVERY_SCHEMA
                and _str(data.get("redis_run_id")) == run_id
                and bool(spec.authority_id)
                and self._authority_key(spec.authority_id) == key
            ):
                specs_by_id[spec.authority_id] = spec
        return [specs_by_id[key] for key in sorted(specs_by_id)]

    async def list_providers(
        self,
        *,
        read_through: Callable[
            [],
            Awaitable[Sequence[AuthorityProviderSpec | Mapping[str, Any]]],
        ]
        | None = None,
    ) -> list[AuthorityProviderSpec]:
        """List current providers, rebuilding from durable source on invalidity."""

        if read_through is None:
            return await self._list_run_fenced_records()
        projected = await self._list_complete_projection()
        if projected is not None:
            return projected
        recovered = [
            value
            if isinstance(value, AuthorityProviderSpec)
            else AuthorityProviderSpec.from_dict(value)
            for value in await read_through()
        ]
        return sorted(
            (spec for spec in recovered if spec.authority_id),
            key=lambda spec: spec.authority_id,
        )

__all__ = [
    "AUTHORITY_DISCOVERY_EPOCH_SCHEMA",
    "AUTHORITY_DISCOVERY_RECONCILE_LOCK_TTL_SECONDS",
    "AUTHORITY_DISCOVERY_PUBLICATION_SCHEMA",
    "AUTHORITY_DISCOVERY_SCHEMA",
    "AuthorityDiscoveryReconcileResult",
    "AuthorityDiscoveryCoordinationError",
    "AuthorityDiscoveryRunChangedError",
    "RedisAuthorityDiscovery",
]

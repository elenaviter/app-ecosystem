# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable invocation policies and idempotency records."""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
from typing import Protocol

from connection_hub.delegated_credentials.durable_io import (
    DurableStorageError,
    list_child_names,
    read_json_or_none,
    write_json_atomic,
)
from connection_hub.invocation_policy.models import (
    InvocationAuthority,
    InvocationPolicy,
    InvocationPolicyChange,
    InvocationPolicyRecordError,
    InvocationRecord,
    validated_invocation_id,
)

POLICIES_DIRNAME = "invocation-policies"
POLICIES_LAYOUT_VERSION = "v1"
GRANTORS_DIRNAME = "grantors"
CARDS_DIRNAME = "cards"
AUTHORITIES_DIRNAME = "authorities"
INVOCATIONS_DIRNAME = "invocations"
POLICY_FILENAME = "policy.json"
POLICY_CHANGE_FILENAME = "policy-change.json"
MUTATION_LOCK_FILENAME = ".mutation.lock"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class InvocationPolicyStorageError(DurableStorageError):
    pass


def owner_hash_for(owner_subject: str) -> str:
    return hashlib.sha256(str(owner_subject or "").encode("utf-8")).hexdigest()


def _validated_hash(value: str, reason: str) -> str:
    digest = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(digest):
        raise InvocationPolicyStorageError(reason)
    return digest


def _validated_access_id(value: str) -> str:
    access_id = str(value or "").strip()
    if not _ACCESS_ID_RE.fullmatch(access_id):
        raise InvocationPolicyStorageError("access_id_invalid")
    return access_id


def invocation_hash_for(invocation_id: str) -> str:
    return hashlib.sha256(validated_invocation_id(invocation_id).encode("utf-8")).hexdigest()


class InvocationPolicyStore(Protocol):
    def authority_path(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> pathlib.Path: ...

    async def read_policy(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> InvocationPolicy | None: ...

    async def write_policy(
        self, *, owner_hash: str, policy: InvocationPolicy
    ) -> None: ...

    async def read_policy_change(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> InvocationPolicyChange | None: ...

    async def write_policy_change(
        self, *, owner_hash: str, change: InvocationPolicyChange
    ) -> None: ...

    async def read_invocation(
        self,
        *,
        owner_hash: str,
        authority: InvocationAuthority,
        invocation_id: str,
    ) -> InvocationRecord | None: ...

    async def write_invocation(
        self, *, owner_hash: str, record: InvocationRecord
    ) -> None: ...

    async def list_policies(
        self, *, owner_hash: str, access_id: str
    ) -> list[InvocationPolicy]: ...


class BundleStorageInvocationPolicyStore:
    def __init__(self, storage_root: str | os.PathLike[str]) -> None:
        self._root = (
            pathlib.Path(storage_root) / POLICIES_DIRNAME / POLICIES_LAYOUT_VERSION
        )

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def card_path(self, *, owner_hash: str, access_id: str) -> pathlib.Path:
        return (
            self._root
            / GRANTORS_DIRNAME
            / _validated_hash(owner_hash, "owner_hash_invalid")
            / CARDS_DIRNAME
            / _validated_access_id(access_id)
        )

    def authority_path(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> pathlib.Path:
        return (
            self.card_path(owner_hash=owner_hash, access_id=authority.access_id)
            / AUTHORITIES_DIRNAME
            / _validated_hash(authority.key, "authority_key_invalid")
        )

    def policy_path(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> pathlib.Path:
        return self.authority_path(owner_hash=owner_hash, authority=authority) / POLICY_FILENAME

    def invocation_path(
        self,
        *,
        owner_hash: str,
        authority: InvocationAuthority,
        invocation_id: str,
    ) -> pathlib.Path:
        return (
            self.authority_path(owner_hash=owner_hash, authority=authority)
            / INVOCATIONS_DIRNAME
            / f"{invocation_hash_for(invocation_id)}.json"
        )

    async def read_policy(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> InvocationPolicy | None:
        payload = await read_json_or_none(
            self.policy_path(owner_hash=owner_hash, authority=authority)
        )
        if payload is None:
            return None
        policy = InvocationPolicy.from_mapping(payload)
        if policy.authority != authority:
            raise InvocationPolicyRecordError("policy_authority_mismatch")
        return policy

    async def write_policy(
        self, *, owner_hash: str, policy: InvocationPolicy
    ) -> None:
        await write_json_atomic(
            self.policy_path(owner_hash=owner_hash, authority=policy.authority),
            policy.to_dict(),
        )

    def policy_change_path(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> pathlib.Path:
        return (
            self.authority_path(owner_hash=owner_hash, authority=authority)
            / POLICY_CHANGE_FILENAME
        )

    async def read_policy_change(
        self, *, owner_hash: str, authority: InvocationAuthority
    ) -> InvocationPolicyChange | None:
        payload = await read_json_or_none(
            self.policy_change_path(owner_hash=owner_hash, authority=authority)
        )
        if payload is None:
            return None
        change = InvocationPolicyChange.from_mapping(payload)
        if change.authority != authority:
            raise InvocationPolicyRecordError("policy_change_authority_mismatch")
        return change

    async def write_policy_change(
        self, *, owner_hash: str, change: InvocationPolicyChange
    ) -> None:
        await write_json_atomic(
            self.policy_change_path(
                owner_hash=owner_hash,
                authority=change.authority,
            ),
            change.to_dict(),
        )

    async def read_invocation(
        self,
        *,
        owner_hash: str,
        authority: InvocationAuthority,
        invocation_id: str,
    ) -> InvocationRecord | None:
        payload = await read_json_or_none(
            self.invocation_path(
                owner_hash=owner_hash,
                authority=authority,
                invocation_id=invocation_id,
            )
        )
        if payload is None:
            return None
        record = InvocationRecord.from_mapping(payload)
        if record.authority != authority:
            raise InvocationPolicyRecordError("invocation_authority_mismatch")
        if record.invocation_id != validated_invocation_id(invocation_id):
            raise InvocationPolicyRecordError("invocation_id_mismatch")
        return record

    async def write_invocation(
        self, *, owner_hash: str, record: InvocationRecord
    ) -> None:
        await write_json_atomic(
            self.invocation_path(
                owner_hash=owner_hash,
                authority=record.authority,
                invocation_id=record.invocation_id,
            ),
            record.to_dict(),
        )

    async def list_policies(
        self, *, owner_hash: str, access_id: str
    ) -> list[InvocationPolicy]:
        root = self.card_path(owner_hash=owner_hash, access_id=access_id) / AUTHORITIES_DIRNAME
        policies: list[InvocationPolicy] = []
        for authority_key in await list_child_names(root):
            try:
                _validated_hash(authority_key, "authority_key_invalid")
            except InvocationPolicyStorageError:
                continue
            payload = await read_json_or_none(root / authority_key / POLICY_FILENAME)
            if payload is None:
                continue
            policy = InvocationPolicy.from_mapping(payload)
            if policy.authority.access_id != access_id or policy.authority.key != authority_key:
                raise InvocationPolicyRecordError("policy_storage_scope_mismatch")
            policies.append(policy)
        policies.sort(
            key=lambda item: (
                item.authority.resource,
                item.authority.surface,
                item.authority.operation,
                item.authority.provider_id,
                item.authority.account_id,
            )
        )
        return policies


__all__ = [
    "MUTATION_LOCK_FILENAME",
    "POLICY_CHANGE_FILENAME",
    "BundleStorageInvocationPolicyStore",
    "InvocationPolicyStorageError",
    "InvocationPolicyStore",
    "invocation_hash_for",
    "owner_hash_for",
]

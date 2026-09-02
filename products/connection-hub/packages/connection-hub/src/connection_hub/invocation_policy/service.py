# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Atomic policy consumption and request-bound replay behavior."""

from __future__ import annotations

import pathlib
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import replace
from typing import Any, Protocol

from connection_hub.invocation_policy.models import (
    INVOCATION_COMPLETED,
    INVOCATION_RESERVED,
    POLICY_ALWAYS,
    POLICY_AVAILABLE,
    POLICY_CHANGE_COMMITTED,
    POLICY_CHANGE_PREPARED,
    POLICY_CONSUMED,
    POLICY_MODES,
    POLICY_ONCE,
    InvocationAuthority,
    InvocationDecision,
    InvocationPolicy,
    InvocationPolicyChange,
    InvocationPolicyRecordError,
    InvocationRecord,
    validated_invocation_id,
    validated_request_digest,
)
from connection_hub.invocation_policy.store import (
    MUTATION_LOCK_FILENAME,
    InvocationPolicyStore,
    owner_hash_for,
)

POLICY_LOCK_WAIT_SECONDS = 30.0


class InvocationPolicyMutationLock(Protocol):
    def __call__(
        self,
        *,
        lock_path: pathlib.Path,
        resource_id: str,
        operation: str,
        wait_seconds: float,
    ) -> AbstractAsyncContextManager[Any]: ...


class InvocationPolicyConflict(RuntimeError):
    def __init__(self, reason: str, *, current_revision: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_revision = current_revision


class InvocationPolicyService:
    def __init__(
        self,
        *,
        store: InvocationPolicyStore,
        mutation_lock: InvocationPolicyMutationLock,
    ) -> None:
        self._store = store
        self._mutation_lock = mutation_lock

    def _critical_section(self, *, owner_hash: str, authority: InvocationAuthority):
        return self._mutation_lock(
            lock_path=(
                self._store.authority_path(owner_hash=owner_hash, authority=authority)
                / MUTATION_LOCK_FILENAME
            ),
            resource_id=f"invocation-policy:{authority.key}",
            operation="invocation-policy-mutation",
            wait_seconds=POLICY_LOCK_WAIT_SECONDS,
        )

    @staticmethod
    def _policy_authorities(
        authority: InvocationAuthority,
    ) -> tuple[InvocationAuthority, ...]:
        """Return every policy scope that can govern an exact invocation."""
        if not authority.provider_id:
            return (authority,)
        general = InvocationAuthority(
            access_id=authority.access_id,
            resource=authority.resource,
            surface=authority.surface,
            operation=authority.operation,
        )
        return tuple(sorted((authority, general), key=lambda item: item.key))

    async def list_for_card(
        self, *, owner_subject: str, access_id: str
    ) -> list[InvocationPolicy]:
        owner = str(owner_subject or "").strip()
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        return await self._store.list_policies(
            owner_hash=owner_hash_for(owner), access_id=access_id
        )

    async def get(
        self, *, owner_subject: str, authority: InvocationAuthority
    ) -> InvocationPolicy | None:
        owner = str(owner_subject or "").strip()
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        return await self._store.read_policy(
            owner_hash=owner_hash_for(owner), authority=authority
        )

    async def set_policy(
        self,
        *,
        owner_subject: str,
        authority: InvocationAuthority,
        mode: str,
        expected_revision: int | None = None,
        now: int | None = None,
    ) -> InvocationPolicy:
        owner = str(owner_subject or "").strip()
        selected_mode = str(mode or "").strip().lower()
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        if selected_mode not in POLICY_MODES:
            raise InvocationPolicyRecordError("policy_mode_invalid")
        owner_hash = owner_hash_for(owner)
        moment = int(now if now is not None else time.time())
        async with self._critical_section(owner_hash=owner_hash, authority=authority):
            change = await self._store.read_policy_change(
                owner_hash=owner_hash,
                authority=authority,
            )
            if change is not None and change.state == POLICY_CHANGE_PREPARED:
                raise InvocationPolicyConflict("invocation_policy_change_in_progress")
            current = await self._store.read_policy(
                owner_hash=owner_hash, authority=authority
            )
            current_revision = current.revision if current is not None else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise InvocationPolicyConflict(
                    "invocation_policy_revision_moved",
                    current_revision=current_revision,
                )
            policy = InvocationPolicy(
                authority=authority,
                mode=selected_mode,
                revision=current_revision + 1,
                state=POLICY_AVAILABLE,
                updated_at=moment,
            )
            await self._store.write_policy(owner_hash=owner_hash, policy=policy)
            return policy

    async def prepare_policy_change(
        self,
        *,
        owner_subject: str,
        authority: InvocationAuthority,
        mode: str,
        change_id: str,
        expected_revision: int | None = None,
        now: int | None = None,
    ) -> InvocationPolicyChange:
        """Place a fail-closed marker before a separate card mutation starts."""
        owner = str(owner_subject or "").strip()
        selected_mode = str(mode or "").strip().lower()
        clean_change_id = validated_invocation_id(change_id)
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        if selected_mode not in POLICY_MODES:
            raise InvocationPolicyRecordError("policy_mode_invalid")
        owner_hash = owner_hash_for(owner)
        moment = int(now if now is not None else time.time())
        async with self._critical_section(owner_hash=owner_hash, authority=authority):
            existing = await self._store.read_policy_change(
                owner_hash=owner_hash,
                authority=authority,
            )
            if existing is not None:
                if (
                    existing.change_id == clean_change_id
                    and existing.mode == selected_mode
                ):
                    return existing
                if existing.state == POLICY_CHANGE_PREPARED:
                    raise InvocationPolicyConflict(
                        "invocation_policy_change_in_progress"
                    )
            current = await self._store.read_policy(
                owner_hash=owner_hash,
                authority=authority,
            )
            current_revision = current.revision if current is not None else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise InvocationPolicyConflict(
                    "invocation_policy_revision_moved",
                    current_revision=current_revision,
                )
            change = InvocationPolicyChange(
                authority=authority,
                change_id=clean_change_id,
                mode=selected_mode,
                state=POLICY_CHANGE_PREPARED,
                expected_policy_revision=current_revision,
                prepared_at=moment,
            )
            await self._store.write_policy_change(
                owner_hash=owner_hash,
                change=change,
            )
            return change

    async def commit_policy_change(
        self,
        *,
        owner_subject: str,
        authority: InvocationAuthority,
        change_id: str,
        now: int | None = None,
    ) -> InvocationPolicy:
        """Publish the selected policy, then make the cross-registry change visible."""
        owner = str(owner_subject or "").strip()
        clean_change_id = validated_invocation_id(change_id)
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        owner_hash = owner_hash_for(owner)
        moment = int(now if now is not None else time.time())
        async with self._critical_section(owner_hash=owner_hash, authority=authority):
            change = await self._store.read_policy_change(
                owner_hash=owner_hash,
                authority=authority,
            )
            if change is None or change.change_id != clean_change_id:
                raise InvocationPolicyConflict("invocation_policy_change_missing")
            current = await self._store.read_policy(
                owner_hash=owner_hash,
                authority=authority,
            )
            current_revision = current.revision if current is not None else 0
            target_revision = change.expected_policy_revision + 1
            if change.state == POLICY_CHANGE_COMMITTED:
                if (
                    current is None
                    or current.revision != change.policy_revision
                    or current.mode != change.mode
                ):
                    raise InvocationPolicyConflict(
                        "invocation_policy_change_commit_moved",
                        current_revision=current_revision,
                    )
                return current
            if current_revision == change.expected_policy_revision:
                current = InvocationPolicy(
                    authority=authority,
                    mode=change.mode,
                    revision=target_revision,
                    state=POLICY_AVAILABLE,
                    updated_at=moment,
                )
                await self._store.write_policy(owner_hash=owner_hash, policy=current)
            elif not (
                current is not None
                and current_revision == target_revision
                and current.mode == change.mode
                and current.state == POLICY_AVAILABLE
            ):
                raise InvocationPolicyConflict(
                    "invocation_policy_change_commit_moved",
                    current_revision=current_revision,
                )
            committed = replace(
                change,
                state=POLICY_CHANGE_COMMITTED,
                policy_revision=target_revision,
                committed_at=moment,
            )
            await self._store.write_policy_change(
                owner_hash=owner_hash,
                change=committed,
            )
            return current

    async def begin(
        self,
        *,
        owner_subject: str,
        authority: InvocationAuthority,
        invocation_id: str = "",
        request_digest: str = "",
        card_revision: int = 0,
        authority_revision: str = "",
        now: int | None = None,
    ) -> InvocationDecision:
        owner = str(owner_subject or "").strip()
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        owner_hash = owner_hash_for(owner)
        moment = int(now if now is not None else time.time())
        policy_authorities = self._policy_authorities(authority)
        async with AsyncExitStack() as stack:
            for policy_authority in policy_authorities:
                await stack.enter_async_context(
                    self._critical_section(
                        owner_hash=owner_hash,
                        authority=policy_authority,
                    )
                )

            exact_policy = await self._store.read_policy(
                owner_hash=owner_hash,
                authority=authority,
            )
            exact_change = await self._store.read_policy_change(
                owner_hash=owner_hash,
                authority=authority,
            )
            if (
                exact_change is not None
                and exact_change.state == POLICY_CHANGE_PREPARED
            ):
                return InvocationDecision(
                    allowed=False,
                    reason="delegated_invocation_policy_changing",
                    dispatch=False,
                    retryable=True,
                    policy=exact_policy,
                )
            policy = exact_policy
            if policy is None and len(policy_authorities) > 1:
                general_authority = next(
                    item for item in policy_authorities if item != authority
                )
                general_change = await self._store.read_policy_change(
                    owner_hash=owner_hash,
                    authority=general_authority,
                )
                if (
                    general_change is not None
                    and general_change.state == POLICY_CHANGE_PREPARED
                ):
                    return InvocationDecision(
                        allowed=False,
                        reason="delegated_invocation_policy_changing",
                        dispatch=False,
                        retryable=True,
                    )
                policy = await self._store.read_policy(
                    owner_hash=owner_hash,
                    authority=general_authority,
                )
            return await self._begin_locked(
                owner_hash=owner_hash,
                authority=authority,
                policy=policy,
                invocation_id=invocation_id,
                request_digest=request_digest,
                card_revision=card_revision,
                authority_revision=authority_revision,
                moment=moment,
            )

    async def _begin_locked(
        self,
        *,
        owner_hash: str,
        authority: InvocationAuthority,
        policy: InvocationPolicy | None,
        invocation_id: str,
        request_digest: str,
        card_revision: int,
        authority_revision: str,
        moment: int,
    ) -> InvocationDecision:
        mode = policy.mode if policy is not None else POLICY_ALWAYS
        if not str(invocation_id or "").strip():
            if mode == POLICY_ONCE:
                return InvocationDecision(
                    allowed=False,
                    reason="delegated_invocation_id_required",
                    dispatch=False,
                    policy=policy,
                )
            return InvocationDecision(
                allowed=True,
                reason="delegated_invocation_allowed",
                dispatch=True,
                policy=policy,
            )

        clean_invocation_id = validated_invocation_id(invocation_id)
        clean_request_digest = validated_request_digest(request_digest)
        existing = await self._store.read_invocation(
            owner_hash=owner_hash,
            authority=authority,
            invocation_id=clean_invocation_id,
        )
        if existing is not None:
            if existing.request_digest != clean_request_digest:
                return InvocationDecision(
                    allowed=False,
                    reason="delegated_invocation_id_conflict",
                    dispatch=False,
                    policy=policy,
                    invocation=existing,
                )
            if existing.state == INVOCATION_COMPLETED:
                return InvocationDecision(
                    allowed=not existing.result_is_error,
                    reason=(
                        "delegated_invocation_replayed"
                        if not existing.result_is_error
                        else "delegated_invocation_terminal_error_replayed"
                    ),
                    dispatch=False,
                    replay=True,
                    policy=policy,
                    invocation=existing,
                )
            return InvocationDecision(
                allowed=False,
                reason="delegated_invocation_outcome_pending",
                dispatch=False,
                replay=True,
                retryable=True,
                policy=policy,
                invocation=existing,
            )

        if (
            policy is not None
            and policy.mode == POLICY_ONCE
            and policy.state == POLICY_CONSUMED
        ):
            return InvocationDecision(
                allowed=False,
                reason="delegated_invocation_limit_exhausted",
                dispatch=False,
                policy=policy,
            )

        policy_id = (
            policy.policy_id
            if policy is not None
            else f"default_{authority.key[:24]}"
        )
        policy_revision = policy.revision if policy is not None else 0
        record = InvocationRecord(
            authority=authority,
            invocation_id=clean_invocation_id,
            request_digest=clean_request_digest,
            policy_id=policy_id,
            policy_revision=policy_revision,
            policy_mode=mode,
            state=INVOCATION_RESERVED,
            card_revision=card_revision,
            authority_revision=str(authority_revision or "").strip(),
            created_at=moment,
        )
        # The reservation is written first. A crash before one-use
        # consumption cannot dispatch this invocation and cannot make its
        # retry dispatch; another new invocation may still consume it.
        await self._store.write_invocation(owner_hash=owner_hash, record=record)
        if policy is not None and policy.mode == POLICY_ONCE:
            policy = replace(
                policy,
                state=POLICY_CONSUMED,
                consumed_invocation_id=clean_invocation_id,
                consumed_request_digest=clean_request_digest,
                consumed_at=moment,
                updated_at=moment,
            )
            await self._store.write_policy(owner_hash=owner_hash, policy=policy)
        return InvocationDecision(
            allowed=True,
            reason="delegated_invocation_allowed",
            dispatch=True,
            policy=policy,
            invocation=record,
        )

    async def complete(
        self,
        *,
        owner_subject: str,
        authority: InvocationAuthority,
        invocation_id: str,
        request_digest: str,
        result: Any,
        result_is_error: bool = False,
        now: int | None = None,
    ) -> InvocationRecord:
        owner = str(owner_subject or "").strip()
        if not owner:
            raise InvocationPolicyRecordError("owner_subject_missing")
        owner_hash = owner_hash_for(owner)
        clean_invocation_id = validated_invocation_id(invocation_id)
        clean_request_digest = validated_request_digest(request_digest)
        moment = int(now if now is not None else time.time())
        async with self._critical_section(owner_hash=owner_hash, authority=authority):
            record = await self._store.read_invocation(
                owner_hash=owner_hash,
                authority=authority,
                invocation_id=clean_invocation_id,
            )
            if record is None:
                raise InvocationPolicyConflict("invocation_reservation_missing")
            if record.request_digest != clean_request_digest:
                raise InvocationPolicyConflict("invocation_request_digest_moved")
            if record.state == INVOCATION_COMPLETED:
                return record
            completed = replace(
                record,
                state=INVOCATION_COMPLETED,
                result=result,
                result_is_error=bool(result_is_error),
                completed_at=moment,
            )
            await self._store.write_invocation(owner_hash=owner_hash, record=completed)
            return completed


__all__ = [
    "POLICY_LOCK_WAIT_SECONDS",
    "InvocationPolicyConflict",
    "InvocationPolicyMutationLock",
    "InvocationPolicyService",
]

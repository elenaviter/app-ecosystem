# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Ordered card mutations over durable revisions and the Redis projection.

Every mutation runs inside a per-card shared critical section. Two mechanisms
serve two different populations:

    critical section   mutual exclusion between writers, held for as long as
                       the owning worker lives
    updating marker    fails readers closed while the transition is in flight,
                       so no request continues under superseded authority

Inside the section the protocol is: read and validate the current durable
revision, bring a projection that fell behind it up to it, install the marker,
write the immutable next revision, advance current.json, then replace the
marker with the committed projection. The marker's fence compares against the
projection, so the repair step makes the fence and the durable check read the
same revision.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
import time
import uuid
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from connection_hub.delegated_credentials.cache_settings import (
    DelegatedCacheSettings,
)
from connection_hub.delegated_credentials.cards.cache import (
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CardAuthority,
    CardCurrentPointer,
    authority_is_usable,
    authority_projection_ttl,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    CardStorageError,
)

_LOGGER = logging.getLogger("connection_hub.delegated_cards.service")

CARD_LOCK_FILENAME = ".mutation.lock"
# Bounds how long a caller waits for another mutation on the same card. The
# lock itself is held for the owner's lifetime, so this only caps the wait.
CARD_LOCK_WAIT_SECONDS = 30.0
BeforeCardCommit = Callable[[], Awaitable[None]]


class CardMutationLockTimeout(TimeoutError):
    """The host could not acquire a card's shared mutation lock in time."""


class CardMutationLock(Protocol):
    """Host capability that serializes one card across workers."""

    def __call__(
        self,
        *,
        lock_path: pathlib.Path,
        resource_id: str,
        operation: str,
        wait_seconds: float,
    ) -> AbstractAsyncContextManager[Any]: ...


# Each held Card section is one open lock file, and a read set holds all of its
# sections at once. Beyond the descriptors already open, keep this many free for
# the rest of the process (sockets, logs, other requests).
LOCK_FD_RESERVE = 64


def _open_descriptors() -> int:
    """How many descriptors the process holds now (Linux /proc/self/fd, macOS /dev/fd)."""
    import os

    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return max(0, len(os.listdir(directory)) - 1)  # the listing's own descriptor
        except OSError:
            continue
    return 0


def _require_lock_budget(sections: int) -> None:
    """Refuse, before taking any section, a set the process cannot hold open at once.

    Running out of descriptors mid-stack would fail with an OS error after some
    sections were taken. This names it up front, counting the descriptors the
    process already holds; the release configuration sets the process's
    open-file limit (RLIMIT_NOFILE), never a request. Nothing was locked or written.
    """
    import resource

    from .transaction_store import CardTransactionRefused

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and sections > soft - _open_descriptors() - LOCK_FD_RESERVE:
        # A definite refusal: the coordinator aborts, nothing is held.
        raise CardTransactionRefused("card_read_set_lock_budget_exceeded")


class CardConflict(RuntimeError):
    """Another mutation owns this card, or its live revision moved."""

    def __init__(self, reason: str, *, current_revision: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_revision = current_revision


class CardCommitFailed(RuntimeError):
    """The durable revision could not be committed; prior authority stands."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CardServingUnavailable(RuntimeError):
    """The revision committed durably; its serving state did not.

    The committed revision is the authority; a read that misses the projection
    reloads it. Carries ``access_id`` so the caller can name the card it holds
    no credential for.
    """

    def __init__(self, reason: str, *, access_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.access_id = access_id


def _serves_after(entry: Any, receipt: Mapping[str, Any]) -> bool:
    """The projection serves exactly this transaction's AFTER: its revision and content, or its tombstone."""

    from ..issuer_gate import change_digest

    if entry is None or entry.card_revision != int(receipt["after"]["card_revision"]):
        return False
    if entry.is_revoked:
        return receipt["after"].get("state") == CARD_STATE_REVOKED
    authority = getattr(entry, "authority", None)
    return bool(entry.is_card and authority is not None
                and change_digest(authority.to_dict()) == receipt["change_digest"])


def transaction_mutation_id(transaction_id: str) -> str:
    """The serving mutation id of one staged transaction: stable across its retries."""

    return "tx-" + str(transaction_id)


class DelegatedCardService:
    def __init__(
        self,
        *,
        store: BundleStorageDelegatedCardStore,
        cache: DelegatedCardRuntimeCache,
        mutation_lock: CardMutationLock,
        settings: DelegatedCacheSettings | None = None,
    ) -> None:
        self._store = store
        self._cache = cache
        self._mutation_lock = mutation_lock
        self._settings = (settings or DelegatedCacheSettings()).cards

    def _lock_path(self, *, subject_hash: str, access_id: str) -> pathlib.Path:
        return (
            self._store.card_path(subject_hash=subject_hash, access_id=access_id)
            / CARD_LOCK_FILENAME
        )

    @asynccontextmanager
    async def _account_binding_mark(self, current: Any, candidate: CardAuthority, *, subject_hash: str):
        """W578: hold this direct write's mark on every account it adds; refuse a disconnect's fence."""
        from .account_fence import AccountFenceRefused, binding_mark

        try:
            async with binding_mark(self._store, current[1] if current is not None else None, candidate,
                                    subject_hash=subject_hash):
                yield
        except AccountFenceRefused as exc:
            raise CardConflict(exc.reason) from None

    async def card_section_held(self, subject_hash: str, access_id: str) -> bool:
        """W578: whether that Card's mutation section is held right now, by the bound production lock."""
        try:
            async with self._mutation_lock(
                lock_path=self._lock_path(subject_hash=subject_hash, access_id=access_id),
                resource_id=f"delegated-card:{access_id}", operation="delegated-card-account-fence-probe",
                wait_seconds=0.5,
            ):
                return False
        except CardMutationLockTimeout:
            return True

    async def reserve_accounts(self, accounts: Any, *, transaction_id: str) -> None:
        """W578: fence these connected accounts for a disconnect transaction (see ``account_fence``)."""
        from .account_fence import AccountFenceRefused, reserve_accounts
        from .transaction_store import CardTransactionRefused

        try:
            await reserve_accounts(self._store, accounts, transaction_id=transaction_id,
                                   probe=self.card_section_held, section=self._account_fence_section)
        except AccountFenceRefused as exc:
            raise CardTransactionRefused(exc.reason) from None
        except CardMutationLockTimeout:
            raise CardTransactionRefused("card_account_reserved") from None

    async def release_accounts(self, transaction_id: str) -> None:
        from .account_fence import release_transaction

        try:
            await release_transaction(self._store, transaction_id, section=self._account_fence_section)
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    def _account_fence_section(self, provider_id: str, account_id: str):
        """W578: one account fence's section of the bound production mutation lock."""
        from .account_fence import FENCES_DIRNAME, account_key

        key = account_key(provider_id, account_id)
        return self._mutation_lock(
            lock_path=self._store.root / FENCES_DIRNAME / key / ".fence.lock",
            resource_id=f"delegated-account-fence:{key}", operation="delegated-account-fence",
            wait_seconds=CARD_LOCK_WAIT_SECONDS,
        )

    async def _mark_staged_binding(self, transaction_id: str, subject_hash: str, original: CardAuthority | None,
                                   candidate: CardAuthority) -> None:
        from .account_fence import AccountFenceRefused, added_accounts, mark_binding

        accounts = added_accounts(original, candidate)
        if not accounts:
            return
        try:
            await mark_binding(self._store, accounts, mark_id=transaction_mutation_id(transaction_id), kind="staged",
                               subject_hash=subject_hash, access_id=candidate.access_id,
                               transaction_id=transaction_id)
        except AccountFenceRefused as exc:
            from .transaction_store import CardTransactionRefused
            raise CardTransactionRefused(exc.reason) from None

    def _clear_staged_binding(self, transaction_id: str, original: CardAuthority | None,
                              candidate: CardAuthority) -> None:
        from .account_fence import added_accounts, clear_binding

        clear_binding(self._store, added_accounts(original, candidate),
                      mark_id=transaction_mutation_id(transaction_id))

    def _clear_staged_binding_from_receipt(self, transaction_id: str, receipt: Mapping[str, Any]) -> None:
        before, after = receipt.get("before"), receipt.get("after")
        if not isinstance(after, Mapping):
            return
        try:
            original = CardAuthority.from_mapping(before) if isinstance(before, Mapping) else None
            candidate = CardAuthority.from_mapping(after)
        except Exception:  # noqa: BLE001 - an unreadable receipt leaves a mark that only goes stale
            return
        self._clear_staged_binding(transaction_id, original, candidate)

    def _critical_section(self, *, subject_hash: str, access_id: str):
        return self._mutation_lock(
            lock_path=self._lock_path(subject_hash=subject_hash, access_id=access_id),
            resource_id=f"delegated-card:{access_id}",
            operation="delegated-card-mutation",
            wait_seconds=CARD_LOCK_WAIT_SECONDS,
        )

    async def commit(
        self,
        authority: CardAuthority,
        *,
        subject_hash: str,
        expected_revision: int,
        now: int | None = None,
        before_commit: BeforeCardCommit | None = None,
    ) -> CardCurrentPointer:
        """Commit the next revision and expose it as live authority."""
        moment = int(now if now is not None else time.time())
        mutation_id = uuid.uuid4().hex
        try:
            async with self._critical_section(
                subject_hash=subject_hash, access_id=authority.access_id
            ):
                await self._assert_no_lifecycle_preparation(subject_hash=subject_hash, access_id=authority.access_id)
                current = await self._assert_expected(
                    subject_hash=subject_hash,
                    access_id=authority.access_id,
                    expected_revision=expected_revision,
                )
                # A host-supplied authority gate runs after lock acquisition
                # and the target CAS check, before projection or durable effects.
                # It must be bounded and must not mutate this target itself.
                if before_commit is not None:
                    await before_commit()
                await self._reconcile(
                    access_id=authority.access_id, current=current, moment=moment
                )
                # W578: a write that ADDS a connected account marks it before
                # the durable commit, so a disconnect fencing that account sees
                # it (and this write sees the disconnect's fence).
                async with self._account_binding_mark(current, authority, subject_hash=subject_hash):
                    await self._mark_updating(
                        access_id=authority.access_id,
                        mutation_id=mutation_id,
                        expected_revision=expected_revision,
                    )
                    pointer = await self._commit_durable(
                        authority=authority, subject_hash=subject_hash, mutation_id=mutation_id
                    )
                try:
                    installed = await self._cache.commit_projection(
                        authority,
                        mutation_id=mutation_id,
                        ttl_seconds=authority_projection_ttl(authority, moment),
                    )
                    if not installed:
                        self._report_uninstalled(
                            access_id=authority.access_id,
                            card_revision=authority.card_revision,
                            transition="projection",
                        )
                    await self._index(
                        authority=authority, subject_hash=subject_hash, moment=moment
                    )
                except Exception as exc:
                    raise CardServingUnavailable(
                        "serving_state_unavailable", access_id=authority.access_id
                    ) from exc
                return pointer
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def stage_transaction(
        self, *, transaction_id: str, intent_digest: str, participant: str, subject_hash: str,
        original: CardAuthority | None, candidate: CardAuthority, now: Any, effects: Any = (), reads: Any = (),
        catalog: str = "", group: Mapping[str, Any] | None = None, collection: Mapping[str, Any] | None = None,
        collection_reads: Any = (),
    ) -> dict[str, Any]:
        """W578: stage one Card of a cross-realm transaction under its mutation fence; nothing is served.

        W502 lane D: ``collection`` and its resolved ``collection_reads`` hold the
        dependencies by reference instead of ``reads`` (transaction_store.stage).

        ``effects`` are the writer's non-Card changes, recorded in the prepared
        receipt and applied only when FINISH materializes a COMMITTED decision
        (W580 F1/F3/F4); an ABORT discards them. Their prepare hook runs after
        the receipt is written, so a failed prepare refuses the stage but the
        Card stays fenced (card_transaction_unresolved) until the coordinator
        records the ABORT and finishes it (Ops 13:05 correction).

        The serving projection is marked updating with the transaction's own
        mutation id BEFORE the first durable write, so no cached resolver
        serves BEFORE while the Card is undecided or after PB committed it
        (Ops F8). When the marker expires, a cache miss reads durable state,
        which refuses while undecided or follows PB's recorded decision. A
        replay keeps its own marker; a refused stage releases it; decide and
        abort finalize it with the same id.
        """
        from .transaction_store import CardTransactionRefused, read_receipt, stage

        mutation_id = transaction_mutation_id(transaction_id)
        access_id = candidate.access_id
        try:
            # W502 read reservations: the candidate's section and every dependency
            # Card's section, taken in one sorted order so stages cannot deadlock.
            async with AsyncExitStack() as sections:
                keys = sorted({(subject_hash, access_id), *((r["subject_hash"], r["access_id"])
                                                            for r in (*(reads or ()), *(collection_reads or ())))})
                _require_lock_budget(len(keys))
                for section_subject, section_access in keys:
                    await sections.enter_async_context(self._critical_section(
                        subject_hash=section_subject, access_id=section_access))
                existing = await read_receipt(self._store, transaction_id)
                if existing is None:
                    # A fresh stage passes the full shared fence (Ops F2); a
                    # replay of this transaction's own prepared receipt is
                    # validated by stage() itself, which this fence would refuse.
                    await self._assert_no_lifecycle_preparation(subject_hash=subject_hash, access_id=access_id)
                if existing is None or existing["state"] == "prepared":
                    try:
                        await self._mark_transaction_updating(
                            access_id=access_id, mutation_id=mutation_id,
                            expected_revision=int(original.card_revision) if original is not None else 0)
                    except Exception as exc:
                        raise CardServingUnavailable("serving_state_unavailable", access_id=access_id) from exc
                # W578: a staged write that ADDS a connected account holds its mark
                # until its own decision, when the binding becomes live.
                await self._mark_staged_binding(transaction_id, subject_hash, original, candidate)
                try:
                    staged = await stage(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                         participant=participant, subject_hash=subject_hash, original=original,
                                         candidate=candidate, now=now, effects=effects, reads=reads,
                                         catalog=catalog, group=group, collection=collection,
                                         collection_reads=collection_reads)
                    await self._run_effect_hook("_effect_preparer", staged, refusal="card_effect_prepare_failed")
                    return staged
                except (CardStorageError, CardTransactionRefused):
                    if existing is None and await read_receipt(self._store, transaction_id) is None:
                        self._clear_staged_binding(transaction_id, original, candidate)
                        # Nothing was staged: release the marker rather than
                        # hold readers closed until it expires.
                        try:
                            await self._cache.finalize_removal(access_id, mutation_id=mutation_id)
                        except Exception:  # noqa: BLE001 - it only expires; readers stay closed
                            pass
                    raise
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def decide_transaction(
        self, *, transaction_id: str, intent_digest: str, decision: str, subject_hash: str, access_id: str,
        reason: str = "", now: int | None = None,
    ) -> dict[str, Any]:
        """W578: materialize the coordinator's recorded decision, keeping serving consistent.

        For COMMITTED the serving projection is marked updating BEFORE the
        receipt rename (the one visibility point), so no resolver serves the
        broader BEFORE as current after a narrowing commit (Ops F3); the
        after-state is installed after it. The serving mutation id is derived
        from the transaction, so a retry owns the marker an earlier attempt
        left. A replay of an already committed decision skips the mark (the
        rename already happened) and re-installs the after-state over its own
        marker, an absent key or an older projection; one already served is
        left as it is, so finishing COMMITTED succeeds (Ops F7). A serving
        failure after the rename raises CardServingUnavailable and the
        decision stands. ABORTED serves nothing new and releases this
        transaction's marker, if one was left.
        """
        from .transaction_store import CardTransactionRefused, decide, read_receipt

        moment = int(now if now is not None else time.time())
        mutation_id = transaction_mutation_id(transaction_id)
        try:
            async with self._critical_section(subject_hash=subject_hash, access_id=access_id):
                receipt = await read_receipt(self._store, transaction_id)
                if decision == "committed" and receipt is not None and receipt["state"] == "prepared":
                    try:
                        await self._mark_transaction_updating(
                            access_id=access_id, mutation_id=mutation_id,
                            expected_revision=int(receipt["before"]["card_revision"]) if receipt["before"] else 0,
                            committing=receipt)
                    except Exception as exc:
                        raise CardServingUnavailable("serving_state_unavailable", access_id=access_id) from exc
                decided = await decide(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                       decision=decision, reason=reason)
                # W578: decided either way, a staged binding is now live or gone; its mark goes.
                self._clear_staged_binding_from_receipt(transaction_id, decided)
                if decided["state"] == "committed" and decided.get("effects"):
                    # Effects before serving: readers refuse until they are applied.
                    from .transaction_store import apply_effects
                    applier = getattr(self, "_effect_applier", None)
                    if applier is None:
                        raise CardServingUnavailable("card_effect_applier_unavailable", access_id=access_id)
                    try:
                        await apply_effects(self._store, decided, applier)
                    except Exception as exc:
                        raise CardServingUnavailable("card_effects_pending", access_id=access_id) from exc
                if decided["state"] != "committed":
                    # An ABORT releases what STAGE prepared (an invocation-policy
                    # marker); a failure leaves the decision standing and the
                    # re-driven FINISH releases again.
                    try:
                        await self._run_effect_hook("_effect_releaser", decided, refusal="card_effects_release_pending")
                    except CardTransactionRefused as exc:
                        raise CardServingUnavailable(str(exc), access_id=access_id) from exc
                    try:
                        await self._cache.finalize_removal(access_id, mutation_id=mutation_id)
                    except Exception:  # noqa: BLE001 - an unreleased marker only expires; readers stay closed
                        pass
                    return decided
                current = await self._store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
                if current is None:
                    raise CardServingUnavailable("serving_state_unavailable", access_id=access_id)
                authority = current[1]
                try:
                    if authority.state == CARD_STATE_REVOKED:
                        # A staged revoke: serve its tombstone, exactly as revoke() does.
                        await self._cache.commit_tombstone(
                            access_id, card_revision=authority.card_revision, mutation_id=mutation_id,
                            ttl_seconds=self._settings.revoked_tombstone_seconds,
                        )
                        await self._cache.index_remove(subject_hash=subject_hash, access_id=access_id)
                    else:
                        await self._cache.commit_projection(
                            authority, mutation_id=mutation_id,
                            ttl_seconds=authority_projection_ttl(authority, moment),
                        )
                        await self._index(authority=authority, subject_hash=subject_hash, moment=moment)
                except Exception as exc:
                    raise CardServingUnavailable("serving_state_unavailable", access_id=access_id) from exc
                return decided
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def stage_group_transaction(
        self, *, transaction_id: str, intent_digest: str, participant: str,
        members: Sequence[tuple[str, CardAuthority | None, CardAuthority, str]], now: Any,
        effects: Any = (), reads: Any = (), catalog: str = "", collection: Mapping[str, Any] | None = None,
        actor_subject: str | None = None, now_epoch: int | None = None,
    ) -> dict[str, Any]:
        """W578: stage a card group (several Cards) under ONE transaction decision.

        ``members`` are ``(subject_hash, original or None, candidate, action)``,
        in the group's canonical (subject_hash, access_id) order. Each member's
        action is checked against its original exactly as the caller-writer
        gate checks a bound write (EMain Q3): only attach binds and only detach
        unbinds, create only for an absent original, update and reset keep
        the protected fields, revoke publishes only an ended state. The
        aggregate receipt is written first, every member is staged under its
        own Card section, and the aggregate is marked staged last; the group's
        reads, catalog and effects ride on its lead member.
        """
        from ..caller_writer_gate import binding_change_refusal, candidate_shape_refusal
        from .model import CARD_STATE_ACTIVE
        from .transaction_store import (
            CardTransactionRefused, begin_group, complete_group, group_member_ref, member_transaction_id,
        )

        ordered = [(str(subject_hash), original, candidate, str(action))
                   for subject_hash, original, candidate, action in members]
        if not ordered:
            raise CardTransactionRefused("card_transaction_group_invalid")
        # W502 lane D: the group's dependencies by reference to the Hub's own sealed collection.
        collection_ref, collection_reads = None, []
        if collection is not None:
            from .card_read_collection import resolve_collection
            if reads:
                raise CardTransactionRefused("card_group_dependencies_mixed")
            try:
                header, collection_reads = await resolve_collection(self._store, collection["collection_id"])
            except CardStorageError as exc:
                raise CardTransactionRefused(str(exc)) from exc
            if (header["root"] != collection["root"] or header["count"] != collection["count"]
                    or header["catalog"] != collection["catalog"] or header["catalog"] != catalog
                    or header["scope"] != collection["scope"] or header["deadline"] != collection["deadline"]
                    or actor_subject is not None and header["actor_subject"] != actor_subject):
                raise CardTransactionRefused("card_read_collection_moved")
            targets = {(str(subject_hash), candidate.access_id) for subject_hash, _, candidate, _ in ordered}
            if targets & {(read["subject_hash"], read["access_id"]) for read in collection_reads}:
                # The group's own Cards are its writes, never its read dependencies.
                raise CardTransactionRefused("card_group_collection_overlaps_target")
            from .transaction_store import read_receipt as _read_receipt
            if (now_epoch is not None and now_epoch >= header["deadline"]
                    and await _read_receipt(self._store, transaction_id) is None):
                raise CardTransactionRefused("card_read_collection_expired")
            collection_ref = {key: collection[key] for key in ("collection_id", "root", "count")}
        for subject_hash, original, candidate, action in ordered:
            before = original.to_dict() if original is not None else None
            after = candidate.to_dict()
            if (original is None) != (action == "create"):
                raise CardTransactionRefused("card_group_member_action_invalid")
            if action == "revoke":
                refusal = (binding_change_refusal(action, before, after)
                           or (None if candidate.state != CARD_STATE_ACTIVE else "card_group_revoke_not_ended"))
            elif action == "recreate":
                # W502: a fresh Card in place of a revoked one (newly invited): same id and
                # grantor, the next revision, and only from a revoked original.
                refusal = (None if (original.state == CARD_STATE_REVOKED and candidate.state == CARD_STATE_ACTIVE
                                    and candidate.access_id == original.access_id
                                    and candidate.grantor_subject == original.grantor_subject
                                    and candidate.card_revision == original.card_revision + 1)
                           else "card_group_recreate_invalid")
            elif action in ("attach", "detach"):
                refusal = binding_change_refusal(action, before, after)
            else:
                refusal = candidate_shape_refusal(action, before, after)
            if refusal is not None:
                raise CardTransactionRefused(refusal)
        # The whole in-group Control graph composes before any write (a planned P
        # and its C and My included); a live parent outside the group is read as is.
        from service_foundation.coordination.durable_decision_log import DecisionRefused

        from .card_group import compose_group_chains, group_member

        async def load_live(subject_hash: str, access_id: str) -> CardAuthority | None:
            found = await self._store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            return None if found is None else found[1]

        try:
            await compose_group_chains([group_member(original=original, candidate=candidate, action=action)
                                        for _, original, candidate, action in ordered], load_live)
        except DecisionRefused as exc:
            raise CardTransactionRefused(str(exc)) from exc
        try:
            # Under the lead member's section: the same section an ABORT of a never
            # staged group takes (abort_unstaged_transaction), so a late begin and
            # that tombstone are ordered, never interleaved.
            async with self._critical_section(subject_hash=ordered[0][0], access_id=ordered[0][2].access_id):
                group = await begin_group(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                          participant=participant,
                                          members=[(subject_hash, candidate.access_id)
                                                   for subject_hash, _, candidate, _ in ordered])
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc
        for index, (subject_hash, original, candidate, _) in enumerate(ordered):
            lead = index == 0
            await self.stage_transaction(
                transaction_id=member_transaction_id(transaction_id, index), intent_digest=intent_digest,
                participant=participant, subject_hash=subject_hash, original=original, candidate=candidate,
                now=now, effects=effects if lead else (), reads=reads if lead else (),
                catalog=catalog if lead else "", group=group_member_ref(group, index),
                collection=collection_ref if lead else None, collection_reads=collection_reads if lead else ())
        return await complete_group(self._store, transaction_id=transaction_id, intent_digest=intent_digest)

    async def decide_group_transaction(
        self, *, transaction_id: str, intent_digest: str, decision: str, reason: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        """W578: materialize the group's one recorded decision on every member, then the aggregate.

        The lead member first: it carries the group's effects, applied before
        any member is served, so no member is served AFTER without them. A
        member never staged (an interrupted prepare) has nothing to decide.
        """
        from .transaction_store import CardTransactionRefused, finish_group, is_group_receipt, read_receipt

        group = await read_receipt(self._store, transaction_id)
        if not is_group_receipt(group):
            raise CardTransactionRefused("card_transaction_unknown")
        if decision == "committed" and not group["staged"]:
            # EMain F1: never commit any member of a group that was not staged as a whole.
            raise CardTransactionRefused("card_transaction_not_staged")
        for member in group["members"]:
            if await read_receipt(self._store, member["transaction_id"]) is None:
                continue
            await self.decide_transaction(
                transaction_id=member["transaction_id"], intent_digest=intent_digest, decision=decision,
                subject_hash=member["subject_hash"], access_id=member["access_id"], reason=reason, now=now)
        finished = await finish_group(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                      decision=decision, reason=reason)
        await self.release_accounts(transaction_id)  # W578: a disconnect's fences end with its decision
        return finished

    async def stage_read_set_transaction(
        self, *, transaction_id: str, intent_digest: str, participant: str, reads: Any = (), catalog: str = "",
    ) -> dict[str, Any]:
        """W578: hold Cards, absences and the active catalog under one decision; write and serve nothing.

        Every read Card's section is taken in one sorted order (as a stage takes
        its dependency sections), each read is verified at its revision and
        fenced, the catalog is reserved, and only then is the receipt written.
        """
        from .transaction_store import prepare_read_set

        try:
            async with AsyncExitStack() as sections:
                keys = sorted({(r["subject_hash"], r["access_id"]) for r in reads or ()})
                _require_lock_budget(len(keys))
                for subject_hash, access_id in keys:
                    await sections.enter_async_context(self._critical_section(
                        subject_hash=subject_hash, access_id=access_id))
                return await prepare_read_set(self._store, transaction_id=transaction_id,
                                              intent_digest=intent_digest, participant=participant,
                                              reads=reads, catalog=catalog)
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def stage_read_collection_transaction(
        self, *, transaction_id: str, intent_digest: str, participant: str, collection_id: str, root: str,
        count: int, catalog: str = "", scope: str | None = None, deadline: int | None = None,
        actor_subject: str | None = None, now: int | None = None,
    ) -> dict[str, Any]:
        """W502 lane D: hold a Hub-sealed collection's Cards, absences and catalog under one decision.

        The reads come from the Hub's own sealed collection, never from the
        initiator's intent; they are taken in the same sorted section order as a
        read set, verified, fenced with this transaction and collection, and only
        then is the bounded receipt (ref, root, count) written.
        """
        from .card_read_collection import resolve_collection
        from .transaction_store import CardTransactionRefused, prepare_read_collection

        try:
            header, reads = await resolve_collection(self._store, collection_id)
        except CardStorageError as exc:
            raise CardTransactionRefused(str(exc)) from exc
        if (header["root"] != root or header["count"] != count or header["catalog"] != catalog
                or scope is not None and header["scope"] != scope
                or deadline is not None and header["deadline"] != deadline
                or actor_subject is not None and header["actor_subject"] != actor_subject):
            raise CardTransactionRefused("card_read_collection_moved")
        if now is not None and now >= header["deadline"]:
            # A first PREPARE never outlives the collection; a replay of a prepared receipt is historical.
            from .transaction_store import read_receipt
            if await read_receipt(self._store, transaction_id) is None:
                raise CardTransactionRefused("card_read_collection_expired")
        try:
            async with AsyncExitStack() as sections:
                keys = sorted({(r["subject_hash"], r["access_id"]) for r in reads})
                _require_lock_budget(len(keys))
                for subject_hash, access_id in keys:
                    await sections.enter_async_context(self._critical_section(
                        subject_hash=subject_hash, access_id=access_id))
                return await prepare_read_collection(self._store, transaction_id=transaction_id,
                                                     intent_digest=intent_digest, participant=participant,
                                                     collection_id=collection_id, root=root, count=count,
                                                     catalog=catalog, reads=reads)
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def decide_read_set_transaction(
        self, *, transaction_id: str, intent_digest: str, decision: str, reason: str = "",
    ) -> dict[str, Any]:
        """W578: the recorded decision releases a read set's fences; nothing was staged, nothing is served."""
        from .transaction_store import finish_read_set

        return await finish_read_set(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                     decision=decision, reason=reason)

    async def stage_effects_transaction(
        self, *, transaction_id: str, intent_digest: str, participant: str, subject_hash: str, effects: Any,
    ) -> dict[str, Any]:
        """W578: an effects-only transaction (card_effects.py): no Card, one owner's bounded effects.

        Inside the section of every account the effects delete (taken in one
        sorted order, the same sections the fence's reserve and release take):
        each account's fence must be this transaction's, so COMMIT can only
        ever apply an effect whose account no new binding can reach; then the
        receipt; then STAGE prepares each effect (account_delete holds the
        exact incarnation, refusing when the stored account is another one).
        A refused preparation leaves the receipt prepared: the initiator's
        ABORT releases whatever was prepared, and the fence.
        """
        from ..durable_io import read_json_or_none
        from .account_fence import fence_holder
        from .card_effects import effect_accounts
        from .transaction_store import CardTransactionRefused, prepare_effects, tombstone_path

        accounts = effect_accounts(effects or ())
        if not accounts:
            raise CardTransactionRefused("card_transaction_effects_invalid")
        try:
            async with AsyncExitStack() as sections:
                for provider_id, account_id in accounts:
                    await sections.enter_async_context(self._account_fence_section(provider_id, account_id))
                if await read_json_or_none(tombstone_path(self._store, transaction_id)) is not None:
                    raise CardTransactionRefused("card_transaction_aborted")  # a late stage after its ABORT
                for provider_id, account_id in accounts:
                    if await fence_holder(self._store, provider_id, account_id) != transaction_id:
                        raise CardTransactionRefused("card_account_not_reserved")
                prepared = await prepare_effects(self._store, transaction_id=transaction_id,
                                                 intent_digest=intent_digest, participant=participant,
                                                 subject_hash=subject_hash, effects=effects)
                await self._run_effect_hook("_effect_preparer", prepared, refusal="card_effect_prepare_failed")
                return prepared
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def decide_effects_transaction(
        self, *, transaction_id: str, intent_digest: str, decision: str, reason: str = "",
    ) -> dict[str, Any]:
        """W578: materialize an effects-only transaction's recorded decision; idempotent; re-driven by recovery.

        COMMITTED applies every effect exactly once (account_delete deletes the
        pinned incarnation and finishes its index and credential cleanup);
        ABORTED releases what STAGE prepared. Only then is the account fence
        released, so no binding can land between the decision and the
        deletion. A failure leaves the decision standing and the fence held,
        and the re-driven FINISH continues from the durable per-effect record.
        """
        from .transaction_store import apply_effects, decide_effects, retire_effects

        decided = await decide_effects(self._store, transaction_id=transaction_id, intent_digest=intent_digest,
                                       decision=decision, reason=reason)
        if decided["state"] == "committed":
            applier = getattr(self, "_effect_applier", None)
            if applier is None:
                raise CardServingUnavailable("card_effect_applier_unavailable", access_id="")
            try:
                await apply_effects(self._store, decided, applier)
            except Exception as exc:
                raise CardServingUnavailable("card_effects_pending", access_id="") from exc
        else:
            await self._run_effect_hook("_effect_releaser", decided, refusal="card_effects_release_pending")
        await self.release_accounts(transaction_id)
        retire_effects(self._store, transaction_id)
        return decided

    async def abort_unstaged_effects(self, *, transaction_id: str, intent_digest: str,
                                     effects: Any) -> dict[str, Any]:
        """W578: tombstone an effects-only transaction never prepared here, then release its fence.

        Under the same account sections its STAGE takes, so a late stage either
        finished first (its receipt is returned and finished through the
        decision) or sees the tombstone and refuses. The fence a crashed
        initiator left behind is released only after the tombstone exists.
        """
        from .card_effects import effect_accounts
        from .transaction_store import abort_unstaged, read_receipt

        try:
            async with AsyncExitStack() as sections:
                for provider_id, account_id in effect_accounts(effects or ()):
                    await sections.enter_async_context(self._account_fence_section(provider_id, account_id))
                existing = await read_receipt(self._store, transaction_id)
                if existing is not None:
                    return existing
                tombstone = await abort_unstaged(self._store, transaction_id, intent_digest=intent_digest)
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc
        await self.release_accounts(transaction_id)
        return tombstone

    async def abort_unstaged_transaction(self, *, transaction_id: str, subject_hash: str,
                                         access_id: str, intent_digest: str = "") -> dict[str, Any]:
        """W581 F1 under the Card's section (Ops B2): tombstone a transaction never prepared here.

        Inside the same section every stage of this Card takes, so an in-flight
        stage either finished first (its receipt exists: returned as is, and
        the caller finishes it through decide) or will see the tombstone and
        refuse. The stage's serving marker, if a crash left it, is released.
        """
        from .transaction_store import abort_unstaged, read_receipt

        try:
            async with self._critical_section(subject_hash=subject_hash, access_id=access_id):
                existing = await read_receipt(self._store, transaction_id)
                if existing is not None:
                    return existing
                tombstone = await abort_unstaged(self._store, transaction_id, intent_digest=intent_digest)
                await self.release_accounts(transaction_id)  # W578: presumed abort releases a crashed fence
                try:
                    await self._cache.finalize_removal(access_id, mutation_id=transaction_mutation_id(transaction_id))
                except Exception:  # noqa: BLE001 - it only expires; readers stay closed meanwhile
                    pass
                return tombstone
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def _run_effect_hook(self, name: str, receipt: Mapping[str, Any], *, refusal: str) -> None:
        """Call the bound prepare/release hook once per recorded effect; every hook is idempotent."""
        from .transaction_store import CardTransactionRefused

        hook = getattr(self, name, None)
        if hook is None or not receipt.get("effects"):
            return
        for effect in receipt["effects"]:
            try:
                await hook(effect["kind"], effect["key"], dict(effect["payload"]),
                           transaction_id=receipt["transaction_id"])
            except Exception as exc:
                raise CardTransactionRefused(refusal) from exc

    def bind_effect_preparer(self, preparer: Any) -> None:
        """``prepare(kind, key, payload, *, transaction_id)`` at STAGE (e.g. a policy marker); idempotent."""
        self._effect_preparer = preparer

    def bind_effect_releaser(self, releaser: Any) -> None:
        """``release(kind, key, payload, *, transaction_id)`` at FINISH(aborted); idempotent."""
        self._effect_releaser = releaser

    def bind_effect_applier(self, applier: Any) -> None:
        """The hosting composition's idempotent ``apply(kind, key, payload, *, transaction_id)``."""
        self._effect_applier = applier

    async def _mark_transaction_updating(self, *, access_id: str, mutation_id: str, expected_revision: int,
                                         committing: Mapping[str, Any] | None = None) -> None:
        """Mark updating, or keep the marker this same transaction already holds.

        When ``committing`` (the prepared receipt of a recorded COMMITTED), a
        projection already serving exactly its AFTER also satisfies the mark:
        a reader restored it from durable state once the stage marker expired
        and PB had decided, so nothing broader can be served (Ops F9).
        """

        try:
            await self._mark_updating(access_id=access_id, mutation_id=mutation_id,
                                      expected_revision=expected_revision)
        except CardConflict:
            entry = await self._cache.read(access_id)
            if entry is not None and entry.is_updating and entry.mutation_id == mutation_id:
                return
            if committing is not None and _serves_after(entry, committing):
                return
            raise

    async def revoke(
        self,
        *,
        subject_hash: str,
        access_id: str,
        expected_revision: int,
        revoked_authority: CardAuthority | None = None,
        before_commit: BeforeCardCommit | None = None,
    ) -> CardCurrentPointer | None:
        """Commit a revoked revision before any credential cleanup.

        Returns ``None`` when the card has no durable history. After the
        revoked revision commits, a failure to clean a credential handle cannot
        restore authority: live resolution observes the revoked state.
        """
        mutation_id = uuid.uuid4().hex
        try:
            async with self._critical_section(
                subject_hash=subject_hash, access_id=access_id
            ):
                await self._assert_no_lifecycle_preparation(subject_hash=subject_hash, access_id=access_id)
                current = await self._store.read_current_authority(
                    subject_hash=subject_hash, access_id=access_id
                )
                if current is None:
                    if before_commit is not None:
                        raise CardConflict("card_revision_moved", current_revision=0)
                    await self._cache.finalize_removal(access_id, mutation_id=mutation_id)
                    await self._cache.index_remove(
                        subject_hash=subject_hash, access_id=access_id
                    )
                    return None
                _, authority = current
                if authority.card_revision != int(expected_revision):
                    raise CardConflict(
                        "card_revision_moved", current_revision=authority.card_revision
                    )
                default_revoked = replace_state(authority, CARD_STATE_REVOKED)
                revoked = revoked_authority or default_revoked
                if (
                    dataclasses.replace(
                        revoked,
                        provenance=default_revoked.provenance,
                    )
                    != default_revoked
                ):
                    raise CardConflict("revoked_authority_invalid")

                if before_commit is not None:
                    await before_commit()
                await self._reconcile(
                    access_id=access_id, current=current, moment=int(time.time())
                )
                await self._mark_updating(
                    access_id=access_id,
                    mutation_id=mutation_id,
                    expected_revision=expected_revision,
                )
                pointer = await self._commit_durable(
                    authority=revoked, subject_hash=subject_hash, mutation_id=mutation_id
                )
                try:
                    installed = await self._cache.commit_tombstone(
                        access_id,
                        card_revision=revoked.card_revision,
                        mutation_id=mutation_id,
                        ttl_seconds=self._settings.revoked_tombstone_seconds,
                    )
                    if not installed:
                        self._report_uninstalled(
                            access_id=access_id,
                            card_revision=revoked.card_revision,
                            transition="tombstone",
                        )
                    await self._cache.index_remove(
                        subject_hash=subject_hash, access_id=access_id
                    )
                except Exception as exc:
                    raise CardServingUnavailable(
                        "serving_state_unavailable", access_id=access_id
                    ) from exc
                return pointer
        except CardMutationLockTimeout as exc:
            raise CardConflict("card_mutation_lock_timeout") from exc

    async def current_revision(self, *, subject_hash: str, access_id: str) -> int:
        """The committed revision whatever its state; 0 with no history. Same
        read as the precondition below.

        This is a writer's precondition read, so it applies the shared fence
        first: a Card with an unresolved transaction or preparation is refused
        here, before the writer makes any side write (a minted credential, an
        invocation policy), not only later at commit (Ops 11:22). The commit
        still rechecks it."""
        await self._assert_no_lifecycle_preparation(subject_hash=subject_hash, access_id=access_id)
        current = await self._store.read_current_authority(
            subject_hash=subject_hash, access_id=access_id
        )
        return int(current[1].card_revision) if current is not None else 0

    async def _assert_expected(
        self, *, subject_hash: str, access_id: str, expected_revision: int
    ) -> tuple[CardCurrentPointer, CardAuthority] | None:
        """The lost-update check reads durable state, not the cache. Returns
        the durable current revision it checked."""
        current = await self._store.read_current_authority(
            subject_hash=subject_hash, access_id=access_id
        )
        held = current[1].card_revision if current is not None else 0
        if held != int(expected_revision):
            raise CardConflict("card_revision_moved", current_revision=held)
        return current

    async def _assert_no_lifecycle_preparation(self, *, subject_hash: str, access_id: str) -> None:
        if not hasattr(self._store, "lifecycle_publish_backend"):
            return  # legacy backend cannot prepare this lifecycle
        from .lifecycle_store import assert_pointer_replaceable

        try:
            await assert_pointer_replaceable(self._store, subject_hash=subject_hash, access_id=access_id)
        except CardStorageError as exc:
            if str(exc) in ("lifecycle_preparation_unresolved", "lifecycle_recovery_queue_unavailable",
                            "issuer_update_preparation_unresolved", "issuer_update_recovery_queue_unavailable",
                            "card_transaction_unresolved"):
                raise CardConflict(str(exc)) from exc
            raise

    @asynccontextmanager
    async def _lifecycle_sections(self, request: Any, actor_subject: str):
        from .lifecycle_store import receipt_path
        from ..durable_io import drain_writes_before_release

        # One forward-progress deadline covers receipt/BOTH Card waits, issuer
        # checks, staging and cleanup. Started mutations drain under the fences;
        # their safe drain can exceed this deadline. Flocks never expire or steal.
        async with asyncio.timeout(CARD_LOCK_WAIT_SECONDS):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self._mutation_lock(
                    lock_path=receipt_path(self._store, request.transaction_id(actor_subject)).with_suffix(".lock"),
                    resource_id=f"delegated-card-lifecycle:{request.transaction_id(actor_subject)}",
                    operation="delegated-card-lifecycle", wait_seconds=CARD_LOCK_WAIT_SECONDS))
                for target in sorted(request.targets, key=lambda item: (item.subject_hash, item.access_id)):
                    await stack.enter_async_context(self._critical_section(
                        subject_hash=target.subject_hash, access_id=target.access_id))
                async with drain_writes_before_release():
                    yield

    async def read_lifecycle_identities(self, request: Any) -> tuple[CardAuthority, CardAuthority]:
        """Internal consistent read, not a public authorization capability.

        Both production fences are held only for raw committed storage reads.
        No cache resolver, repair, marker, receipt, handle or issuer I/O runs
        here. Unresolved preparation requires a separate authorized recovery.
        """
        from ..issuer_read import IssuerReadRefused, IssuerReadRequest, _request_valid

        if type(request) is not IssuerReadRequest or not _request_valid(request):
            raise IssuerReadRefused("issuer_read_request_invalid")
        if (getattr(self._store, "lifecycle_publish_backend", "") != "filesystem-atomic-rename"
                or getattr(self._store, "lifecycle_lock_scope", "") not in ("same-host-flock", "shared-flock-verified")):
            raise IssuerReadRefused("issuer_read_atomic_fences_unavailable")
        try:
            async with asyncio.timeout(CARD_LOCK_WAIT_SECONDS):
                async with AsyncExitStack() as stack:
                    for target in sorted(request.targets, key=lambda t: (t.subject_hash, t.access_id)):
                        await stack.enter_async_context(self._critical_section(
                            subject_hash=target.subject_hash, access_id=target.access_id))
                    authorities = []
                    for target in request.targets:
                        await self._assert_no_lifecycle_preparation(subject_hash=target.subject_hash, access_id=target.access_id)
                        current = await self._store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
                        authority = None if current is None else current[1]
                        if authority is None:
                            raise IssuerReadRefused("issuer_read_target_missing")
                        if (authority.access_id, authority.grantor_subject, authority.issuer_kind, authority.issuer_ref) != (
                                target.access_id, target.owner_subject, target.issuer_kind, target.issuer_ref):
                            raise IssuerReadRefused("issuer_read_target_binding_invalid")
                        authorities.append(authority)
                    return tuple(authorities)
        except CardConflict as exc:
            raise IssuerReadRefused("issuer_read_lifecycle_pending", retryable=True) from exc
        except (TimeoutError, CardMutationLockTimeout) as exc:
            raise IssuerReadRefused("issuer_read_timeout", retryable=True) from exc

    @asynccontextmanager
    async def _issuer_update_sections(self, query, actor_subject):
        from .update_store import receipt_path
        from ..durable_io import drain_writes_before_release

        async with asyncio.timeout(CARD_LOCK_WAIT_SECONDS):
            async with AsyncExitStack() as stack:
                transaction_id = query.transaction_id(actor_subject)
                await stack.enter_async_context(self._mutation_lock(
                    lock_path=receipt_path(self._store, transaction_id).with_suffix(".lock"),
                    resource_id=f"delegated-card-issuer-update:{transaction_id}",
                    operation="delegated-card-issuer-update", wait_seconds=CARD_LOCK_WAIT_SECONDS))
                await stack.enter_async_context(self._critical_section(
                    subject_hash=query.target.subject_hash, access_id=query.target.access_id))
                async with drain_writes_before_release():
                    yield

    async def update_issuer(self, query, *, actor_subject, before_commit):
        """Exact widening update under receipt and production Card fences.

        No handle changes. Started file/Redis mutations drain before either
        fence releases, even on timeout or cancellation. Replay can finish
        serving state, but never authorizes or applies another Card revision.
        """
        from . import update_store as updates
        from ..issuer_update import IssuerUpdateQuery, IssuerUpdateRefused, build_candidate
        from ..issuer_gate import IssuerWriteRefused, change_digest
        from ..durable_io import cancellation_safe_await

        if type(query) is not IssuerUpdateQuery or IssuerUpdateQuery.from_mapping(query.to_dict()) != query:
            raise IssuerUpdateRefused("issuer_update_query_invalid")
        if (not callable(before_commit)
                or getattr(self._store, "lifecycle_publish_backend", "") != "filesystem-atomic-rename"
                or getattr(self._store, "lifecycle_lock_scope", "") not in ("same-host-flock", "shared-flock-verified")):
            raise IssuerUpdateRefused("issuer_update_atomic_fences_unavailable")
        transaction_id = query.transaction_id(actor_subject)

        def deadline(value):
            now = datetime.now(timezone.utc)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise IssuerUpdateRefused("issuer_decision_expiry_invalid")
            if not 0 < (value - now).total_seconds() <= 60:
                raise IssuerUpdateRefused("issuer_decision_expired")
            return value

        try:
            async with self._issuer_update_sections(query, actor_subject):
                recorded = await updates.read_receipt(self._store, transaction_id)
                if recorded is not None:
                    if recorded["binding"] != query.binding(actor_subject):
                        raise IssuerUpdateRefused("issuer_update_replay_changed")
                    recorded = await updates.recover_prepared(self._store, recorded)
                    return await self._finish_issuer_update(recorded)
                await self._assert_no_lifecycle_preparation(
                    subject_hash=query.target.subject_hash, access_id=query.target.access_id)
                candidate = None
                try:
                    current = await self._store.read_current_authority(
                        subject_hash=query.target.subject_hash, access_id=query.target.access_id)
                    original = None if current is None else current[1]
                    candidate = build_candidate(original, query)
                    if current[0].content_hash != original.content_hash():
                        raise IssuerUpdateRefused("issuer_update_legacy_payload_requires_migration")
                    initial_deadline = deadline(await before_commit(original, candidate))
                except (IssuerUpdateRefused, IssuerWriteRefused) as exc:
                    return await updates.refuse_unprepared(self._store, query, actor_subject, exc.reason,
                        candidate_digest=change_digest(candidate.to_dict()) if candidate is not None else "")
                await cancellation_safe_await(self._reconcile(
                    access_id=original.access_id, current=current, moment=int(time.time())))

                async def claim():
                    await cancellation_safe_await(self._mark_updating(access_id=original.access_id,
                        mutation_id=transaction_id, expected_revision=original.card_revision))

                async def publish_gate():
                    fresh_deadline = deadline(await before_commit(original, candidate))
                    entry = await self._cache.read(original.access_id)
                    if entry is None or not entry.is_updating or entry.mutation_id != transaction_id:
                        raise CardConflict("issuer_update_serving_fence_lost")
                    return min(initial_deadline, fresh_deadline)

                try:
                    recorded = await updates.atomic_update(self._store, query=query, actor_subject=actor_subject,
                        original=original, candidate=candidate, now=datetime.now(timezone.utc),
                        after_prepare=claim, before_publish=publish_gate)
                except Exception:
                    recorded = await updates.read_receipt(self._store, transaction_id)
                    if recorded is None or recorded["state"] == "prepared":
                        raise
                return await self._finish_issuer_update(recorded)
        except (TimeoutError, CardMutationLockTimeout) as exc:
            recorded = await updates.read_receipt(self._store, transaction_id)
            if recorded is not None:
                return recorded  # committed/pending is never a no-write refusal
            raise CardConflict("issuer_update_timeout") from exc

    async def _finish_issuer_update(self, receipt):
        from . import update_store as updates
        from ..issuer_update import IssuerUpdateQuery
        from ..durable_io import cancellation_safe_await

        if receipt["serving_state"] != "pending":
            # A crash or unlink error after the terminal receipt rename may
            # leave the bounded active queue populated. Identical recovery
            # retries only retirement, never a mutation or serving rewrite.
            await updates.retire(self._store, receipt)
            return receipt
        query = IssuerUpdateQuery.from_mapping(receipt["binding"]["request"])
        target = query.target
        try:
            if receipt["state"] == "refused":
                if not await cancellation_safe_await(self._cache.finalize_removal(
                        target.access_id, mutation_id=receipt["transaction_id"])):
                    # An expired marker may already have been read-through
                    # restored to BEFORE. That is a finished no-write outcome,
                    # not an intent that should block every later writer.
                    entry = await self._cache.read(target.access_id)
                    if (entry is None or not entry.is_card or entry.authority is None
                            or entry.authority.card_revision != target.expected_card_revision
                            or entry.authority.content_hash() != target.expected_authority_fingerprint):
                        return receipt
            else:
                current = await self._store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
                if current is None or current[0].to_dict() != receipt["after"]:
                    return receipt
                authority = current[1]
                installed = await cancellation_safe_await(self._cache.commit_projection(authority,
                    mutation_id=receipt["transaction_id"], ttl_seconds=authority_projection_ttl(authority, int(time.time()))))
                if not installed:
                    entry = await self._cache.read(target.access_id)
                    if entry is None or not entry.is_card or entry.authority != authority:
                        return receipt
                await cancellation_safe_await(self._index(authority=authority,
                    subject_hash=target.subject_hash, moment=int(time.time())))
            return await updates.mark_serving_complete(self._store, receipt)
        except Exception:
            _LOGGER.warning("[connection-hub] issuer update serving pending transaction=%s", receipt["transaction_id"])
            return receipt

    async def revoke_lifecycle(self, request: Any, *, actor_subject: str,
            before_commit: Callable[[tuple[CardAuthority, CardAuthority]], Awaitable[datetime]],
            after_commit: Callable[[tuple[CardAuthority, CardAuthority]], Awaitable[None]]) -> dict[str, Any]:
        """Generic pair primitive, NOT an authenticated public entrypoint.

        Host supplies the protected actor and configured issuer closure.
        Persistence supplies idempotent handle cleanup. Receipt and both real
        per-Card fences remain held through checks, staging and cleanup. A
        durable committed/pending outcome is NEVER a no-write refusal.
        """
        from .lifecycle import LifecycleRefused, LifecycleRequest
        from . import lifecycle_store as lifecycle
        from ..durable_io import cancellation_safe_await

        if not isinstance(request, LifecycleRequest) or not callable(before_commit) or not callable(after_commit):
            raise LifecycleRefused("issuer_lifecycle_commit_gate_unavailable")
        if (getattr(self._store, "lifecycle_publish_backend", "") != "filesystem-atomic-rename"
                or getattr(self._store, "lifecycle_lock_scope", "") not in ("same-host-flock", "shared-flock-verified")):
            raise LifecycleRefused("issuer_lifecycle_atomic_fences_unavailable")
        transaction_id = request.transaction_id(actor_subject)
        access_ids = tuple(target.access_id for target in request.targets)
        try:
            async with self._lifecycle_sections(request, actor_subject):
                recorded = await lifecycle.read_receipt(self._store, transaction_id)
                if recorded is not None:
                    if recorded["binding"] != request.binding(actor_subject):
                        raise LifecycleRefused("issuer_lifecycle_replay_changed")
                    if recorded["state"] == "prepared":
                        recorded = await lifecycle.abort_prepared(self._store, request=request, actor_subject=actor_subject)
                    return await self._finish_lifecycle(recorded, after_commit=after_commit)
                authorities = []
                for target in request.targets:
                    await self._assert_no_lifecycle_preparation(subject_hash=target.subject_hash, access_id=target.access_id)
                    current = await self._store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
                    target.assert_authority(None if current is None else current[1])
                    authorities.append(current[1])
                authorities = tuple(authorities)
                def valid_deadline(value):
                    now = datetime.now(timezone.utc)
                    if not isinstance(value, datetime) or value.utcoffset() is None:
                        raise LifecycleRefused("issuer_decision_expiry_invalid")
                    if value <= now or (value - now).total_seconds() > 60:
                        raise LifecycleRefused("issuer_decision_expired")
                    return value

                initial_deadline = valid_deadline(await before_commit(authorities))
                from .cache import CardCacheUnusable
                try:
                    await self._cache.require_lifecycle_backend()
                except CardCacheUnusable as exc:
                    reason = "issuer_" + exc.reason
                    await lifecycle.refuse_before_prepare(self._store, request=request, actor_subject=actor_subject,
                                                         now=datetime.now(timezone.utc), reason=reason)
                    raise LifecycleRefused(reason) from exc
                for authority, target in zip(authorities, request.targets):
                    current = await self._store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
                    await cancellation_safe_await(self._reconcile(access_id=authority.access_id, current=current, moment=int(time.time())))

                async def claim():
                    if not await cancellation_safe_await(self._cache.claim_lifecycle(authorities, mutation_id=transaction_id)):
                        raise CardConflict("card_lifecycle_transition_not_claimed")

                async def publish_gate():
                    deadline = min(initial_deadline, valid_deadline(await before_commit(authorities)))
                    if not await self._cache.lifecycle_fenced(access_ids, mutation_id=transaction_id):
                        raise CardConflict("card_lifecycle_serving_fence_lost")
                    return deadline

                try:
                    receipt = await lifecycle.atomic_revoke(self._store, request=request, actor_subject=actor_subject,
                        now=datetime.now(timezone.utc), before_publish=publish_gate, after_prepare=claim)
                except Exception:
                    receipt = await lifecycle.read_receipt(self._store, transaction_id)
                    if receipt is not None and receipt["state"] == "committed":
                        return await self._finish_lifecycle(receipt, after_commit=after_commit)
                    if receipt is not None and receipt["state"] == "refused":
                        await self._finish_lifecycle(receipt, after_commit=after_commit)
                    raise
                return await self._finish_lifecycle(receipt, after_commit=after_commit)
        except (TimeoutError, CardMutationLockTimeout) as exc:
            recorded = await lifecycle.read_receipt(self._store, transaction_id)
            if recorded is not None and recorded["state"] == "committed":
                return recorded  # recovery required; never claim no-write
            raise CardConflict("card_lifecycle_timeout") from exc

    async def _finish_lifecycle(self, receipt: dict[str, Any], *, after_commit: Any) -> dict[str, Any]:
        from . import lifecycle_store as lifecycle
        from ..durable_io import cancellation_safe_await

        if receipt["serving_state"] != "pending":
            return receipt  # stable replay, including after a later legitimate revision
        mutation_id = receipt["transaction_id"]
        access_ids = tuple(entry["access_id"] for entry in receipt["targets"])
        if receipt["state"] == "refused":
            await cancellation_safe_await(self._cache.release_lifecycle(access_ids, mutation_id=mutation_id))
            return await lifecycle.mark_serving_complete(self._store, receipt)
        try:
            before = []
            for entry in receipt["targets"]:
                current = await self._store.read_current(subject_hash=entry["subject_hash"], access_id=entry["access_id"])
                if current is None or current.to_dict() != entry["after"]:
                    return receipt  # do NOT remove handles belonging to another revision
                authority = await self._store.read_revision(subject_hash=entry["subject_hash"], access_id=entry["access_id"],
                    revision_name=entry["before"]["revision_name"])
                if authority is None:
                    return receipt
                before.append(authority)
            await cancellation_safe_await(after_commit(tuple(before)))
            if not await cancellation_safe_await(self._cache.finish_lifecycle(tuple(before), mutation_id=mutation_id)):
                return receipt  # both persistent markers remain; no success relabelling
            for entry in receipt["targets"]:
                await cancellation_safe_await(self._cache.index_remove(subject_hash=entry["subject_hash"], access_id=entry["access_id"]))
            return await lifecycle.mark_serving_complete(self._store, receipt)
        except Exception:
            _LOGGER.warning("[connection-hub.delegated-cards] lifecycle committed; serving pending transaction=%s",
                            mutation_id, exc_info=True)
            return receipt

    async def _reconcile(
        self,
        *,
        access_id: str,
        current: tuple[CardCurrentPointer, CardAuthority] | None,
        moment: int,
    ) -> None:
        """Bring a projection older than the durable revision up to it.

        Why: Redis can lose writes the durable store kept. On 2026-09-21 three
        projections restarted one revision behind their durable Cards, and
        every reconnect then failed the marker fence with
        ``card_transition_not_claimed`` because the fence compared the
        projection while the precondition had checked the durable revision.
        Runs inside the critical section, after the durable check, so the
        revision it installs is the one this mutation fences on.
        """
        if current is None:
            return
        _, durable = current
        usable = authority_is_usable(durable, moment)
        repaired = await self._cache.reconcile_projection(
            access_id,
            durable_revision=durable.card_revision,
            authority=durable if usable else None,
            ttl_seconds=authority_projection_ttl(durable, moment) if usable else None,
        )
        if repaired:
            _LOGGER.warning(
                "[connection-hub.delegated-cards] projection behind durable card=%s "
                "revision=%s; repaired before the fence",
                access_id,
                durable.card_revision,
            )

    async def _mark_updating(
        self, *, access_id: str, mutation_id: str, expected_revision: int
    ) -> None:
        """Close readers for the transition. The section already excludes other
        writers, so a refusal here means the live value is a marker or tombstone
        this mutation must not displace."""
        claimed = await self._cache.claim_transition(
            access_id,
            mutation_id=mutation_id,
            expected_revision=expected_revision,
            ttl_seconds=self._settings.updating_marker_seconds,
        )
        if claimed:
            return
        entry = await self._cache.read(access_id)
        raise CardConflict(
            "card_transition_not_claimed",
            current_revision=entry.card_revision if entry is not None else 0,
        )

    async def _commit_durable(
        self, *, authority: CardAuthority, subject_hash: str, mutation_id: str
    ) -> CardCurrentPointer:
        moment = datetime.now(timezone.utc)
        try:
            pointer = await self._store.write_revision(
                subject_hash=subject_hash, authority=authority, updated_at=moment
            )
            await self._store.advance_current(subject_hash=subject_hash, pointer=pointer)
        except Exception as exc:
            # Nothing committed, or a revision written without becoming current.
            # Drop the marker so the previously committed revision serves again.
            await self._release(authority.access_id, mutation_id=mutation_id)
            raise CardCommitFailed("durable_commit_failed") from exc
        return pointer

    @staticmethod
    def _report_uninstalled(
        *, access_id: str, card_revision: int, transition: str
    ) -> None:
        """The durable revision committed; the serving projection refused it.

        Every surviving refusal leaves a live entry that is not stale-permissive
        — an equal or newer revision, a revoked tombstone, another mutation's
        marker, or an unusable value that denies and reloads from durable.
        Logged because the return value is otherwise unobserved.
        """
        _LOGGER.warning(
            "[connection-hub.delegated-cards] %s not installed card=%s revision=%s; "
            "live projection is equal or newer",
            transition,
            access_id,
            card_revision,
        )

    async def _release(self, access_id: str, *, mutation_id: str) -> None:
        try:
            await self._cache.finalize_removal(access_id, mutation_id=mutation_id)
        except Exception:
            _LOGGER.warning(
                "[connection-hub.delegated-cards] marker release failed card=%s",
                access_id,
                exc_info=True,
            )

    async def _index(
        self, *, authority: CardAuthority, subject_hash: str, moment: int
    ) -> None:
        ttl_seconds = authority_projection_ttl(authority, moment)
        if authority.state != CARD_STATE_ACTIVE or (
            ttl_seconds is not None and ttl_seconds <= 0
        ):
            await self._cache.index_remove(
                subject_hash=subject_hash, access_id=authority.access_id
            )
            return
        await self._cache.index_add(
            subject_hash=subject_hash,
            access_id=authority.access_id,
            expires_at=(None if ttl_seconds is None else moment + ttl_seconds),
        )


def replace_state(authority: CardAuthority, state: str) -> CardAuthority:
    """The same authority at the next revision, in a new lifecycle state."""
    return CardAuthority(
        access_id=authority.access_id,
        client_id=authority.client_id,
        grantor_subject=authority.grantor_subject,
        delegate_subject=authority.delegate_subject,
        source=authority.source,
        card_kind=authority.card_kind,
        label=authority.label,
        card_revision=authority.card_revision + 1,
        catalog_version=authority.catalog_version,
        state=state,
        operations=authority.operations,
        resource_grants=authority.resource_grants,
        resource_operations=authority.resource_operations,
        named_service_operations=authority.named_service_operations,
        named_services=authority.named_services,
        account_scope=authority.account_scope,
        identity_scope=authority.identity_scope,
        created_at=authority.created_at,
        expires_at=authority.expires_at,
        last_issued_at=authority.last_issued_at,
        last_four=authority.last_four,
        resource_acceptance=authority.resource_acceptance,
        provenance=authority.provenance,
        entry_resource=authority.entry_resource,
        client_metadata=authority.client_metadata,
        control_card=authority.control_card,
        issuer_ref=authority.issuer_ref,
        issuer_kind=authority.issuer_kind,
        issuer_label=authority.issuer_label,
        manage_url=authority.manage_url,
        composition_mode=authority.composition_mode,
        properties=authority.properties,
    )


__all__ = [
    "CARD_LOCK_WAIT_SECONDS",
    "CardCommitFailed",
    "CardConflict",
    "CardMutationLock",
    "CardMutationLockTimeout",
    "CardServingUnavailable",
    "DelegatedCardService",
    "replace_state",
]

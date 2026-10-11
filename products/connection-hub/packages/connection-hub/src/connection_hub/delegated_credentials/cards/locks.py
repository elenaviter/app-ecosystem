# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 K1: where the Card mutation lock lives.

K1 (EMain, 10 Oct 18:31Z, operator's "K1 permit"): flock on the live /bundle-storage share (Docker Desktop
file sharing) let 1 overlap through in 600 acquisitions. The Card files stay on the share; the LOCK must not.

``container_local_lock`` (option a) keeps the existing flock but puts each lock file on a container-local
path (a tmpfs such as /run/kdcube-card-locks), where flock is the kernel's own and excludes every process of
that container. It does NOT exclude a writer in another container or host on the same share: Ops found one
(chat-ingress loads the Hub bundle and its on_bundle_load repairs legacy Cards), so (a) is only the local
layer. EMain 18:40Z: phase 1 pairs it with ``proc_only_lock``: a Card mutation lock is granted only in the
platform's "proc" process role, so a Card writer anywhere else (chat-ingress) fails loudly instead of racing.
PostgreSQL advisory locks (option b) follow before W661 Done.

10 Oct, live defect and operator binding (verbatim: "read kdcube docs on bundle storage", "pb is a bundle"): the
/run root was an invented host path that the app user (uid 1000) could not create, so every Card write failed.
The lock root is now the bundle's own storage root from the platform plus ``_card_locks``
(sdk/bundle/bundle-storage-and-cache-README.md: "use self.bundle_storage_root() and create a subdirectory below
it"). With option R the per-Card Redis lock (redis_lock.py) is the cross-container guard and this file lock the
second layer, the critical-section doc's git-bundle order.

The role signal is the platform's GATEWAY_COMPONENT: the SDK sets "proc" in apps/chat/proc/web_app.py and
"ingress" in apps/chat/ingress/web_app.py (os.environ.setdefault, before the workers spawn). It is not unique
to chat-proc (apps/metrics/web_app.py also defaults to "proc"; Infra 18:57Z), so the guard is correct only
while exactly one proc-labelled container can reach the Card share (live: chat-proc; metrics has no
/bundle-storage mount).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

from .service import CardMutationLock
from .store import CardStorageError

CONTAINER_LOCK_SUFFIX = ".lock"
CARD_LOCK_SUBDIR = "_card_locks"


def card_lock_root(bundle_storage_root: os.PathLike[str] | str | None) -> pathlib.Path:
    """The Card file-lock root: the bundle's platform storage root plus ``_card_locks``; none refuses by name."""
    if bundle_storage_root is None or not os.fspath(bundle_storage_root):
        raise CardStorageError("card_lock_root_unavailable")
    return pathlib.Path(os.path.abspath(os.fspath(bundle_storage_root))) / CARD_LOCK_SUBDIR
PROCESS_ROLE_ENV = "GATEWAY_COMPONENT"
CARD_WRITER_ROLE = "proc"


def container_local_lock_path(root: os.PathLike[str] | str, lock_path: os.PathLike[str] | str) -> pathlib.Path:
    """One fixed local file per shared lock path: the same shared path names the same local file in every
    process of the container (an absolute path, never resolved through links)."""
    shared = os.path.abspath(os.fspath(lock_path))
    return pathlib.Path(root) / f"{hashlib.sha256(shared.encode('utf-8')).hexdigest()}{CONTAINER_LOCK_SUFFIX}"


def container_local_lock(base_lock: CardMutationLock, root: os.PathLike[str] | str) -> CardMutationLock:
    """``base_lock`` (the SDK flock) taken on a container-local file instead of the shared lock path."""
    local_root = pathlib.Path(root)
    if not local_root.is_absolute():
        raise ValueError("card_lock_root_not_absolute")

    @asynccontextmanager
    async def lock(*, lock_path: pathlib.Path, resource_id: str, operation: str,
                   wait_seconds: float) -> AsyncIterator[Any]:
        async with AsyncExitStack() as stack:
            try:
                held = await stack.enter_async_context(base_lock(
                    lock_path=container_local_lock_path(local_root, lock_path), resource_id=resource_id,
                    operation=operation, wait_seconds=wait_seconds))
            except OSError as exc:  # e.g. the process user cannot create the lock root: refuse by name
                raise CardStorageError("card_lock_root_unwritable") from exc
            yield held

    return lock


def current_process_role() -> str:
    """The platform's process role, read at each call ("" when unset: never treated as proc)."""
    return (os.environ.get(PROCESS_ROLE_ENV) or "").strip().lower()


def proc_only_lock(base_lock: CardMutationLock, *, role: Any = current_process_role,
                   allowed: str = CARD_WRITER_ROLE) -> CardMutationLock:
    """Grant the Card mutation lock only in the Card-writer process role; elsewhere refuse before acquiring.

    Every Card mutation takes this lock first, so a write outside chat-proc refuses
    card_store_write_wrong_process_role (fail closed: an unset role is not proc).
    """

    @asynccontextmanager
    async def lock(*, lock_path: pathlib.Path, resource_id: str, operation: str,
                   wait_seconds: float) -> AsyncIterator[Any]:
        if role() != allowed:
            raise CardStorageError("card_store_write_wrong_process_role")
        async with base_lock(lock_path=lock_path, resource_id=resource_id, operation=operation,
                             wait_seconds=wait_seconds) as held:
            yield held

    return lock


def refuse_outside_proc(role: Any = current_process_role, allowed: str = CARD_WRITER_ROLE) -> Any:
    """The store-level guard: a Card file write outside the proc role refuses before any byte is written."""

    def guard() -> None:
        if role() != allowed:
            raise CardStorageError("card_store_write_wrong_process_role")
    return guard


def guard_card_store_writes(store: Any, *, role: Any = current_process_role) -> Any:
    """Register the store's root with durable_io: every JSON write under it (versions, current.json, markers,
    receipts, inflight files) is proc-only. Returns the store."""
    from ..durable_io import guard_writes_under

    guard_writes_under(store.root, refuse_outside_proc(role))
    return store


class CardRowOperation:
    """W661 option P under R (operator 21:50Z "R now, with P"): one Card operation's view of its fenced rows.

    The storage-level optimistic check (kdcube docs service/comm/data-bus-README.md, "Ordering And Concurrency":
    "still require storage-level idempotency and optimistic concurrency checks"; "the storage layer must reject
    stale updates") sits UNDER whatever mutation lock serializes the normal case: the container-local flock
    (lock_backend local) or the per-Card KDCube Redis cluster lock (observed_redis_lock_async, option R).

    It holds no connection and no lock: each row statement runs on its own pool connection, and a write CASes
    from the row_revision THIS operation read (``observed``). ``resources`` are the lock resource ids this
    operation holds, so first touch (adoption of a pre-cutover file) happens only under the Card's own lock.
    """

    def __init__(self) -> None:
        import uuid

        # The row fence (Infra's post-owner-guard HOLD): a locked read writes this id into the row it reads, and
        # every CAS of this operation requires it, so the latest LOCKED reader owns the row and a stalled writer
        # whose lock was taken over can no longer move it, whatever its Redis check said before the stall.
        self.op_id = uuid.uuid4().hex
        self.observed: dict[tuple, int] = {}
        self.resources: dict[str, int] = {}
        self.closed = False

    def holds(self, resource_id: str) -> bool:
        return self.resources.get(resource_id, 0) > 0 and not self.closed

    def check_open(self) -> None:
        if self.closed:
            # A child task that outlived its operation never acts on the operation's observations.
            raise CardStorageError("card_lock_session_closed")


_ROW_OPERATION: ContextVar["CardRowOperation | None"] = ContextVar("w661_card_row_operation", default=None)


def current_lock_session() -> "CardRowOperation | None":
    """This Card operation's row context (b-core reads and CASes through it), or None outside a Card lock."""
    operation = _ROW_OPERATION.get()
    if operation is not None:
        operation.check_open()
    return operation


def row_fenced_lock(base_lock: CardMutationLock) -> CardMutationLock:
    """``base_lock`` plus this operation's row context: the outermost lock section opens it, nested sections of
    the same operation share it (their resources add up), and it closes when the outermost section ends."""

    @asynccontextmanager
    async def lock(*, lock_path: pathlib.Path, resource_id: str, operation: str,
                   wait_seconds: float) -> AsyncIterator[Any]:
        current = _ROW_OPERATION.get()
        owner = current is None or current.closed
        context = CardRowOperation() if owner else current
        token = _ROW_OPERATION.set(context) if owner else None
        try:
            async with base_lock(lock_path=lock_path, resource_id=resource_id, operation=operation,
                                 wait_seconds=wait_seconds) as held:
                context.resources[resource_id] = context.resources.get(resource_id, 0) + 1
                try:
                    yield held
                finally:
                    context.resources[resource_id] -= 1
                    if not context.resources[resource_id]:
                        del context.resources[resource_id]
        finally:
            if owner:
                context.closed = True
                _ROW_OPERATION.reset(token)

    return lock


def hub_card_mutation_lock(base_lock: CardMutationLock, root: os.PathLike[str] | str,
                           *, role: Any = current_process_role) -> CardMutationLock:
    """Phase 1 (EMain 18:40Z): proc-only, on a container-local lock file; with option P's row context, so pointer
    and marker rows are fenced whenever they are configured (inert without rows)."""
    return row_fenced_lock(proc_only_lock(container_local_lock(base_lock, root), role=role))


def hub_redis_card_mutation_lock(redis: Any, base_lock: CardMutationLock,
                                 root: os.PathLike[str] | str, *, tenant: str, project: str,
                                 observed_lock: Any, make_metadata: Any, role: Any = current_process_role,
                                 **settings: Any) -> CardMutationLock:
    """Option R (operator 10 Oct, "R now, with P"): proc-only, then the per-Card KDCube Redis lock, then the
    container-local observed file lock (the critical-section doc's git-bundle order: Redis, then the file lock).
    Option P's row context wraps the stack: a stall past the Redis TTL is refused by the row CAS."""
    from .redis_lock import redis_card_mutation_lock

    stack = redis_card_mutation_lock(redis, observed_lock=observed_lock, make_metadata=make_metadata, tenant=tenant,
                                     project=project, base_lock=container_local_lock(base_lock, root), **settings)
    return row_fenced_lock(proc_only_lock(stack, role=role))


__all__ = ["CARD_LOCK_SUBDIR", "CARD_WRITER_ROLE", "CONTAINER_LOCK_SUFFIX", "CardRowOperation", "PROCESS_ROLE_ENV",
           "card_lock_root", "current_lock_session", "row_fenced_lock",
           "container_local_lock", "container_local_lock_path", "current_process_role", "guard_card_store_writes",
           "hub_card_mutation_lock", "hub_redis_card_mutation_lock", "proc_only_lock", "refuse_outside_proc"]

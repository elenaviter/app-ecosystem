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

The role signal is the platform's GATEWAY_COMPONENT: the SDK sets "proc" in apps/chat/proc/web_app.py and
"ingress" in apps/chat/ingress/web_app.py (os.environ.setdefault, before the workers spawn).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .service import CardMutationLock
from .store import CardStorageError

CONTAINER_LOCK_SUFFIX = ".lock"
DEFAULT_CARD_LOCK_ROOT = "/run/kdcube-card-locks"
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
        async with base_lock(lock_path=container_local_lock_path(local_root, lock_path), resource_id=resource_id,
                             operation=operation, wait_seconds=wait_seconds) as held:
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


def hub_card_mutation_lock(base_lock: CardMutationLock, root: os.PathLike[str] | str = DEFAULT_CARD_LOCK_ROOT,
                           *, role: Any = current_process_role) -> CardMutationLock:
    """Phase 1 (EMain 18:40Z): proc-only, on a container-local lock file."""
    return proc_only_lock(container_local_lock(base_lock, root), role=role)


__all__ = ["CARD_WRITER_ROLE", "CONTAINER_LOCK_SUFFIX", "DEFAULT_CARD_LOCK_ROOT", "PROCESS_ROLE_ENV",
           "container_local_lock", "container_local_lock_path", "current_process_role", "guard_card_store_writes",
           "hub_card_mutation_lock", "proc_only_lock", "refuse_outside_proc"]

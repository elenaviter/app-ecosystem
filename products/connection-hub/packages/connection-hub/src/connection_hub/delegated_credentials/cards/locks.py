# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 K1: where the Card mutation lock lives.

K1 (EMain, 10 Oct 18:31Z, operator's "K1 permit"): flock on the live /bundle-storage share (Docker Desktop
file sharing) let 1 overlap through in 600 acquisitions. The Card files stay on the share; the LOCK must not.

``container_local_lock`` (option a) keeps the existing flock but puts each lock file on a container-local
path (a tmpfs such as /run/kdcube-card-locks), where flock is the kernel's own and excludes every process of
that container. It does NOT exclude a writer in another container or host on the same share: Ops found one
(chat-ingress loads the Hub bundle and its on_bundle_load repairs legacy Cards), so (a) is only the local
layer; a cross-container lock (option b) is EMain's decision.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .service import CardMutationLock

CONTAINER_LOCK_SUFFIX = ".lock"


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


__all__ = ["CONTAINER_LOCK_SUFFIX", "container_local_lock", "container_local_lock_path"]

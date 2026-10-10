# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Atomic JSON object IO for durable delegated-access storage.

Shared by the catalog and card stores. Absence, unreadability, and malformed
content are three distinct outcomes so callers can answer "confirmed absent"
and "unavailable" differently.

Filesystem work runs off the event loop. Publication is a temporary file
followed by a same-directory rename, which is atomic on local filesystems and
on shared mounts such as EFS.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Mapping

_DEFERRED_WRITES: ContextVar[list[asyncio.Task] | None] = ContextVar("delegated_lifecycle_writes", default=None)
_PUBLISH_BEFORE: ContextVar[datetime | None] = ContextVar("delegated_lifecycle_publish_before", default=None)


async def cancellation_safe_await(operation):
    """Finish a started lifecycle mutation before its fences can be released.

    Used for Redis transitions and credential cleanup as well as file writes.
    Outside the lifecycle scope this preserves ordinary await semantics.
    """
    pending = _DEFERRED_WRITES.get()
    if pending is None:
        return await operation
    task = asyncio.create_task(operation)
    pending.append(task)
    return await asyncio.shield(task)


@asynccontextmanager
async def drain_writes_before_release():
    """Lifecycle-only cancellation safety; enter INSIDE all mutation fences.

    Cancelling to_thread does not stop its syscall. No mutation fence may be
    released until every started write has returned. A hung backend can exceed
    the logical operation deadline; never pretend cancellation made it safe.
    """
    tasks = []
    token = _DEFERRED_WRITES.set(tasks)
    try:
        yield
    finally:
        try:
            if tasks:
                completion = asyncio.gather(*tasks, return_exceptions=True)
                while not completion.done():
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        continue
        finally:
            _DEFERRED_WRITES.reset(token)


@contextmanager
def require_publish_before(deadline: datetime):
    if not isinstance(deadline, datetime) or deadline.utcoffset() is None:
        raise DurableStorageError("issuer_decision_expiry_invalid")
    token = _PUBLISH_BEFORE.set(deadline)
    try:
        yield
    finally:
        _PUBLISH_BEFORE.reset(token)


class DurableStorageError(RuntimeError):
    """Durable storage could not be read or written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DurableDecodeError(ValueError):
    """A durable object exists but is not usable JSON."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def read_json_or_none(path: pathlib.Path) -> Any | None:
    """Parsed JSON, or ``None`` when the object is confirmed absent."""
    result = await asyncio.to_thread(_read_text, path)
    if result is None:
        return None
    if isinstance(result, OSError):
        raise DurableStorageError("read_failed")
    try:
        return json.loads(result)
    except ValueError as exc:
        raise DurableDecodeError("object_not_json") from exc


async def write_json_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    try:
        await cancellation_safe_await(asyncio.to_thread(_write_text_atomic, path, text))
    except OSError as exc:
        raise DurableStorageError("write_failed") from exc


async def list_child_names(path: pathlib.Path) -> list[str]:
    """Sorted child names, or an empty list when the directory is absent."""
    result = await asyncio.to_thread(_list_children, path)
    if isinstance(result, OSError):
        raise DurableStorageError("list_failed")
    return result


async def path_is_file(path: pathlib.Path) -> bool:
    return await asyncio.to_thread(path.is_file)


def _read_text(path: pathlib.Path) -> str | OSError | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc


def _fsync_directory(directory: pathlib.Path) -> None:
    """Make a rename in ``directory`` durable (W661: "There is no fsync"). Best effort where unsupported."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems refuse fsync on a directory; the file itself is already fsynced
    finally:
        os.close(fd)


def _write_text_atomic(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}")
    try:
        # W661: the bytes are durable before the rename makes them visible, and the rename is durable
        # before the call returns (temp file, fsync, rename, fsync of the directory).
        tmp_path.write_text(text, encoding="utf-8")
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            os.fsync(fd)  # fsync flushes the file itself, whichever descriptor names it
        finally:
            os.close(fd)
        deadline = _PUBLISH_BEFORE.get()
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise DurableStorageError("issuer_decision_expired")
        tmp_path.replace(path)
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def _list_children(path: pathlib.Path) -> list[str] | OSError:
    try:
        return sorted(child.name for child in path.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        return exc


__all__ = [
    "DurableDecodeError",
    "DurableStorageError",
    "list_child_names",
    "path_is_file",
    "read_json_or_none",
    "write_json_atomic",
]

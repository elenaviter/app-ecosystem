"""A test guard: blocking work called on a running event loop's thread is a violation (W461).

Every channel of a host shares one relay event loop, so a blocking call
there stalls them all (2026-10-02: 134 loop blocks of 3 s or more on one
host, and a 15 s stall that dropped every Data Bus socket at once). The
guard wraps the blocking primitives the relay uses: the field store, the
coordinate queue, host config loading, git and subprocesses, and the
workspace walk. A call made while an event loop runs in the calling
thread is recorded with the relay frame that made it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import subprocess
import threading
import traceback
from typing import Any, Callable

from project_board.client import coordinate_queue, host_config, store, workspace_size, worktree_files


def _relay_frame() -> str:
    for frame in reversed(traceback.extract_stack()[:-3]):
        if "/project_board/client/" in frame.filename and not frame.filename.endswith(("store.py", "coordinate_queue.py", "host_config.py", "worktree_files.py", "workspace_size.py")):
            return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
    return "?"


_DEPTH = threading.local()


# Named, reviewed exceptions only. Each states why it is not yet off the loop.
EXEMPT = {
    # W321: the outbox claim runs on the loop with a non-blocking lock so a
    # turn cancelled while waiting claims nothing. Moving it to a thread needs
    # a release of claimed rows on cancellation; that design is its own item.
    ("SharedFieldStore.pull_outbox", "relay.py:_flush_outbox_unlocked"),
}


class LoopGuard:
    def __init__(self, monkeypatch: Any) -> None:
        self.violations: list[tuple[str, str]] = []
        self._monkeypatch = monkeypatch

    def _wrap(self, owner: Any, name: str, label: str) -> None:
        original = getattr(owner, name)
        if isinstance(inspect.getattr_static(owner, name), (staticmethod, classmethod)):
            return
        if inspect.iscoroutinefunction(original):
            return

        @functools.wraps(original)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            depth = getattr(_DEPTH, "value", 0)
            # Only the outermost guarded call is the relay's; the store's own
            # helpers inside it are the same violation.
            if depth == 0 and asyncio._get_running_loop() is not None:
                frame = _relay_frame()
                file_and_function = frame.split(":")[0] + ":" + frame.rsplit(":", 1)[-1]
                if (label, file_and_function) not in EXEMPT:
                    self.violations.append((label, frame))
            _DEPTH.value = depth + 1
            try:
                return original(*args, **kwargs)
            finally:
                _DEPTH.value = depth

        self._monkeypatch.setattr(owner, name, guarded)

    def install(self) -> "LoopGuard":
        for name, value in vars(store.SharedFieldStore).items():
            if callable(value) and not name.startswith("__"):
                self._wrap(store.SharedFieldStore, name, f"SharedFieldStore.{name}")
        for name, value in vars(coordinate_queue.CoordinateQueue).items():
            if callable(value) and not name.startswith("__"):
                self._wrap(coordinate_queue.CoordinateQueue, name, f"CoordinateQueue.{name}")
        load = host_config.HostRelayConfig.load

        def guarded_load(path: Any) -> Any:
            if asyncio._get_running_loop() is not None:
                self.violations.append(("HostRelayConfig.load", _relay_frame()))
            return load(path)

        self._monkeypatch.setattr(host_config.HostRelayConfig, "load", staticmethod(guarded_load))
        self._wrap(subprocess, "run", "subprocess.run")
        self._wrap(worktree_files, "_git", "worktree_files._git")
        self._wrap(workspace_size, "directory_bytes", "workspace_size.directory_bytes")
        return self

    def report(self) -> list[str]:
        seen: dict[tuple[str, str], int] = {}
        for item in self.violations:
            seen[item] = seen.get(item, 0) + 1
        return [f"{count}x {label} from {frame}" for (label, frame), count in sorted(seen.items())]

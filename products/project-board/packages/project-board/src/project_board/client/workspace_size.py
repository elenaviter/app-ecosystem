"""The size of each agent workspace, measured off the relay (W423, W461).

W423 reports an agent's workspace size on its heartbeat, so a filling disk is
seen before it is full. The size needs a walk over every file of the
workspace, and those are large: 0.5 to 3.6 GB on dev-main on 2026-10-02.

Two defects of the first version are the reason for this module:

- The walk state lived on the relay adapter that ``poll_attendances_once``
  builds anew for every attended project on every poll. The next poll saw no
  state, so every heartbeat of every channel started a full walk, the 900 s
  interval never applied, and the size never reached a heartbeat. Each walk
  ran in ``asyncio.to_thread``, on the default executor that also runs the
  OAuth profile lock acquires and DNS lookups (W461).
- Moved to one serial thread, the walks were still Python running inside the
  relay process, competing with the event loop for the GIL around the clock.

Here the state lives in one ``WorkspaceSizes`` held by the long-lived relay
supervisor and handed to every adapter, keyed by workspace path. A walk runs
in a child process (``python -m project_board.client.workspace_size <path>``),
so it shares neither the event loop, the default executor nor the GIL. At
most ``max_concurrent_walks`` walks run at once per relay (two by default),
each path at most once per ``interval_seconds``, and each logs its timing.

Why two and not one (W469): with one walk at a time, every channel's walk at
relay startup queued behind the slowest workspace. On dev-main on 2026-10-02
a 0.9 GB workspace of many small files walked for 87 s, and the walks behind
it waited up to 142 s. With two slots, a long walk holds one slot and the
others pass through the second. The bound keeps disk load to two child
processes. Two long walks at once still delay the rest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("project_board.client.relay")

REMEASURE_SECONDS = 900.0
WALK_TIMEOUT_SECONDS = 600.0
WALK_SLOW_SECONDS = 10.0
MAX_CONCURRENT_WALKS = 2


def directory_bytes(root: Path) -> int:
    """Bytes under ``root``, never following links, skipping what cannot be read."""

    total = 0
    for current, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                pass
    return total


class WorkspaceWalkFailed(RuntimeError):
    """The child walk exited non-zero; carries its exit code and exception type, never its text."""

    def __init__(self, returncode: int | None, exception_type: str) -> None:
        super().__init__(f"workspace walk exited {returncode} ({exception_type})")
        self.returncode = returncode
        self.exception_type = exception_type


# The exceptions a standard-library directory walk in a fresh interpreter can
# end with. Anything else, including any text the child printed, is "unknown".
_KNOWN_WALK_EXCEPTIONS = frozenset(
    {
        "BlockingIOError",
        "FileNotFoundError",
        "ImportError",
        "InterruptedError",
        "IsADirectoryError",
        "KeyboardInterrupt",
        "MemoryError",
        "ModuleNotFoundError",
        "NotADirectoryError",
        "OSError",
        "PermissionError",
        "RecursionError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "ValueError",
    }
)


def _exception_type(stderr: bytes) -> str:
    """A known exception class name from a traceback's last line, else "unknown".

    Only a name from ``_KNOWN_WALK_EXCEPTIONS`` leaves this function: the
    child's stderr is arbitrary text and may name paths (W461 review).
    """

    lines = [line for line in stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return "unknown"
    name = lines[-1].split(":", 1)[0].strip()
    return name if name in _KNOWN_WALK_EXCEPTIONS else "unknown"


async def _reap_through_cancellation(process: asyncio.subprocess.Process) -> None:
    """Wait for a killed child to exit, even when the caller is cancelled again.

    The walk slot is released when the walk returns. A second cancellation
    while waiting for the killed child would otherwise release the slot while
    the child was still unreaped (W469 review). The child was sent SIGKILL, so
    the wait is short. Further cancellations are absorbed here, and the
    caller's original exception is raised after.
    """

    reaped = asyncio.ensure_future(process.wait())
    while not reaped.done():
        try:
            await asyncio.shield(reaped)
        except asyncio.CancelledError:
            continue


async def walk_in_child_process(path: str, *, timeout_seconds: float = WALK_TIMEOUT_SECONDS) -> int:
    """``directory_bytes`` of ``path``, computed by a child Python process."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "project_board.client.workspace_size",
        path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await _reap_through_cancellation(process)
        raise
    if process.returncode != 0:
        raise WorkspaceWalkFailed(process.returncode, _exception_type(err))
    return int(out.decode("ascii").strip())


@dataclass
class _Entry:
    size: int | None = None
    measured_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class WorkspaceSizes:
    """Last measured size per workspace path, remeasured off the relay at most once per interval."""

    def __init__(
        self,
        *,
        interval_seconds: float = REMEASURE_SECONDS,
        walk: Callable[[str], Awaitable[int]] | None = None,
        clock: Callable[[], float] | None = None,
        max_concurrent_walks: int = MAX_CONCURRENT_WALKS,
    ) -> None:
        if max_concurrent_walks < 1:
            raise ValueError("max_concurrent_walks must be at least 1")
        self.interval_seconds = float(interval_seconds)
        self.max_concurrent_walks = int(max_concurrent_walks)
        self._walk = walk or walk_in_child_process
        self._clock = clock or time.monotonic
        self._entries: dict[str, _Entry] = {}
        # Created on first use, inside the relay's running loop.
        self._walk_slots: asyncio.Semaphore | None = None

    def last(self, path: str) -> int | None:
        entry = self._entries.get(path)
        return None if entry is None else entry.size

    def schedule(self, path: str, *, worker_name: str = "-") -> asyncio.Task[None] | None:
        """Start a measurement of ``path`` when it is due; never waits for it."""

        entry = self._entries.setdefault(path, _Entry())
        if entry.task is not None and not entry.task.done():
            return None
        if entry.measured_at is not None and self._clock() - entry.measured_at < self.interval_seconds:
            return None
        entry.task = asyncio.get_running_loop().create_task(
            self._measure(path, entry, worker_name), name="problem-board-workspace-size"
        )
        return entry.task

    async def _measure(self, path: str, entry: _Entry, worker_name: str) -> None:
        if self._walk_slots is None:
            self._walk_slots = asyncio.Semaphore(self.max_concurrent_walks)
        queued_at = self._clock()
        async with self._walk_slots:
            started = self._clock()
            outcome = "ok"
            try:
                entry.size = await self._walk(path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the last size stays, the next interval retries
                outcome = type(exc).__name__
                if isinstance(exc, WorkspaceWalkFailed):
                    outcome = f"{outcome}:{exc.returncode}:{exc.exception_type}"
            ended = self._clock()
            entry.measured_at = ended
        walk_seconds = ended - started
        logger.log(
            logging.WARNING if walk_seconds >= WALK_SLOW_SECONDS or outcome != "ok" else logging.INFO,
            "Problem Board workspace size walk worker=%s outcome=%s walk_seconds=%.3f "
            "queue_wait_seconds=%.3f bytes=%s",
            worker_name,
            outcome,
            walk_seconds,
            started - queued_at,
            entry.size if entry.size is not None else "-",
        )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m project_board.client.workspace_size <path>", file=sys.stderr)
        return 2
    print(directory_bytes(Path(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

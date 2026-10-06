"""Each relay channel's last attendance poll and last success, for its readers (W456).

W456 criterion 6: "each channel's last attendance poll, last success and
backoff are visible on the board or in `pb worker inspect`". The backoff is the
pacing record (``relay_pacing``), kept only while a channel fails. This module
keeps the other two, per channel, in the relay's memory, and the relay writes a
snapshot to ``relay-channels.json`` beside the host config at most every
``WRITE_INTERVAL_SECONDS``, in a thread: no file I/O on the event loop for it
(criterion 4). The file holds times, outcomes and codes, never a secret.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

CHANNEL_STATUS_FILENAME = "relay-channels.json"
CHANNEL_STATUS_SCHEMA = "problem-board.relay-channel-status.v1"
WRITE_INTERVAL_SECONDS = 15.0


def _iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class ChannelStatusBook:
    """The relay's in-memory record; every method is a dictionary update."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._channels: dict[str, dict[str, Any]] = {}
        self._changed = False
        self._written_at = 0.0

    def note_attendance_poll(self, name: str) -> None:
        self._channels.setdefault(name, {})["last_attendance_poll_at"] = self._clock()
        self._changed = True

    def note_turn(self, name: str, outcome: str, code: str = "") -> None:
        record = self._channels.setdefault(name, {})
        now = self._clock()
        record["last_turn_at"] = now
        record["last_outcome"] = str(outcome or "")
        record["last_code"] = str(code or "")
        if outcome == "succeeded":
            record["last_success_at"] = now
        self._changed = True

    def forget(self, name: str) -> None:
        if self._channels.pop(name, None) is not None:
            self._changed = True

    def due(self) -> bool:
        return self._changed and self._clock() - self._written_at >= WRITE_INTERVAL_SECONDS

    def take_snapshot(self) -> dict[str, Any]:
        """The record to write now; marks it written."""

        now = self._clock()
        self._changed = False
        self._written_at = now
        return {
            "schema": CHANNEL_STATUS_SCHEMA,
            "recorded_at": _iso(now),
            "channels": {
                name: {
                    "last_attendance_poll_at": _iso(record.get("last_attendance_poll_at")),
                    "last_success_at": _iso(record.get("last_success_at")),
                    "last_turn_at": _iso(record.get("last_turn_at")),
                    "last_outcome": str(record.get("last_outcome") or ""),
                    "last_code": str(record.get("last_code") or ""),
                }
                for name, record in sorted(self._channels.items())
            },
        }


def write_channel_status(config_path: str | Path, snapshot: Mapping[str, Any]) -> None:
    """Replace the file atomically; run it in a thread."""

    path = Path(config_path).expanduser().parent / CHANNEL_STATUS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def channel_status(config_path: str | Path, worker_name: str) -> dict[str, Any] | None:
    """One channel's recorded status, with when the relay wrote it; None when absent."""

    path = Path(config_path).expanduser().parent / CHANNEL_STATUS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    channels = data.get("channels") if isinstance(data, Mapping) else None
    record = channels.get(worker_name) if isinstance(channels, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    return {**dict(record), "recorded_at": str(data.get("recorded_at") or "")}


__all__ = [
    "CHANNEL_STATUS_FILENAME",
    "ChannelStatusBook",
    "WRITE_INTERVAL_SECONDS",
    "channel_status",
    "write_channel_status",
]

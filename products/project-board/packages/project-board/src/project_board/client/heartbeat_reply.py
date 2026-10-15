"""One channel's process-memory heartbeat snapshots, never action authority."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Mapping

from ..contract.heartbeat_reply import (
    REPLY_PROTOCOL, MAX_KNOWN_BYTES, canonical_bytes, decode_reply, digest, knowledge,
)


class ReplyDecodeError(ValueError):
    """A response cannot be safely materialized; the next request must be full."""


@dataclass(frozen=True)
class ReplyRequest:
    project_ref: str
    context: tuple[Any, ...] | None
    sequence: int
    baseline: dict[str, Any] | None = None
    scope: str = ""
    revision: str = ""

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"reply_protocol": REPLY_PROTOCOL}
        if self.context is not None and self.baseline is not None and digest(self.baseline) == self.revision:
            known = knowledge(self.baseline, self.scope)
            if len(canonical_bytes(known)) <= MAX_KNOWN_BYTES:
                result["known_reply"] = known
        return result


@dataclass(frozen=True)
class _Saved:
    scope: str
    revision: str
    snapshot: dict[str, Any]


class ReplySnapshots:
    """Retain full values with their identities; no bare revision survives loss."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._context: tuple[Any, ...] | None = None
        self._sequence = 0
        self._latest: dict[str, int] = {}
        self._saved: dict[str, _Saved] = {}

    def begin(self, project_ref: str, context: tuple[Any, ...] | None) -> ReplyRequest:
        with self._lock:
            if context is None or context != self._context:
                self._saved.clear()
                self._latest.clear()
                self._context = context
            self._sequence += 1
            self._latest[project_ref] = self._sequence
            saved = self._saved.get(project_ref)
            if saved is not None:
                try:
                    intact = digest(saved.snapshot) == saved.revision
                except (TypeError, ValueError, OverflowError, RecursionError):
                    intact = False
                if not intact:
                    self._saved.pop(project_ref, None)
                    saved = None
            return ReplyRequest(project_ref, context, self._sequence,
                                deepcopy(saved.snapshot) if saved else None,
                                saved.scope if saved else "", saved.revision if saved else "")

    def forget(self, project_ref: str) -> None:
        with self._lock:
            self._saved.pop(project_ref, None)
            self._latest.pop(project_ref, None)

    def retain_projects(self, project_refs: set[str]) -> None:
        with self._lock:
            for project_ref in set(self._latest) - (project_refs | {""}):
                self.forget(project_ref)

    def apply(self, request: ReplyRequest, response: Mapping[str, Any], *,
              context: tuple[Any, ...] | None,
              materialize: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with self._lock:
            if request.context != context or self._context != context or self._latest.get(request.project_ref) != request.sequence:
                return None
            try:
                if request.baseline is not None and digest(request.baseline) != request.revision:
                    raise ReplyDecodeError("Local heartbeat request baseline changed")
                try:
                    snapshot, scope = decode_reply(response, baseline=request.baseline, scope=request.scope)
                except (TypeError, ValueError, KeyError, IndexError, OverflowError, RecursionError) as exc:
                    raise ReplyDecodeError("Heartbeat reconstruction requires a full reply") from exc
                candidate = deepcopy(snapshot)
                materialize(deepcopy(snapshot))
            except BaseException:
                self.forget(request.project_ref)
                raise
            # A reentrant materializer may have invalidated attendance. Never
            # revive a discarded scope after it returns.
            if self._latest.get(request.project_ref) == request.sequence and context is not None and scope:
                self._saved[request.project_ref] = _Saved(scope, digest(candidate), candidate)
            else:
                self._saved.pop(request.project_ref, None)
            return deepcopy(candidate)

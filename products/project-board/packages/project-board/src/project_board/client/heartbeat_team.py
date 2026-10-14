"""Process-local TEAM snapshot/token pairs with request and channel fencing.

    These snapshots are display evidence only. They confer no Card, parent,
    grant, action eligibility, attendance or mail-send authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Mapping

from project_board.contract.heartbeat_team import TEAM_PROTOCOL, content_digest, revision_parts


@dataclass(frozen=True)
class TeamRequest:
    project_ref: str
    context: tuple[Any, ...] | None
    sequence: int
    known_revision: str = ""

    def payload(self) -> dict[str, str]:
        if self.context is None:
            return {}
        result = {"team_protocol": TEAM_PROTOCOL}
        if self.known_revision:
            result["known_team_revision"] = self.known_revision
        return result


@dataclass(frozen=True)
class _Snapshot:
    revision: str
    team: list[dict[str, Any]]


class TeamSnapshots:
    """One channel's coherent process-memory pairs; restart starts without any."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._context: tuple[Any, ...] | None = None
        self._sequence = 0
        self._latest: dict[str, int] = {}
        self._snapshots: dict[str, _Snapshot] = {}

    def begin(self, project_ref: str, context: tuple[Any, ...] | None) -> TeamRequest:
        with self._lock:
            if context is None or context != self._context:
                self._snapshots.clear()
                self._latest.clear()
                self._context = context
            self._sequence += 1
            self._latest[project_ref] = self._sequence
            saved = self._snapshots.get(project_ref)
            if saved is not None and not self._intact(saved):
                self._snapshots.pop(project_ref, None)
                saved = None
            return TeamRequest(project_ref, context, self._sequence, saved.revision if saved else "")

    @staticmethod
    def _intact(saved: _Snapshot) -> bool:
        parts = revision_parts(saved.revision)
        try:
            return parts is not None and parts[1] == content_digest(saved.team)
        except (TypeError, ValueError, OverflowError):
            return False

    def forget(self, project_ref: str) -> None:
        with self._lock:
            self._snapshots.pop(project_ref, None)
            self._latest.pop(project_ref, None)

    def retain_projects(self, project_refs: set[str]) -> None:
        with self._lock:
            for project_ref in set(self._latest) - project_refs:
                self.forget(project_ref)

    def apply(
        self, request: TeamRequest, response: Mapping[str, Any], *,
        context: tuple[Any, ...] | None,
        materialize: Callable[[list[dict[str, Any]]], None],
    ) -> str:
        """Materialize before advancing a token, under the same local fence.

        The callback runs on the channel's existing store thread. A late
        request cannot replace a newer request's display. A failed callback
        invalidates conditional knowledge and retains no bare token.
        """
        with self._lock:
            if self._latest.get(request.project_ref) != request.sequence or request.context != context or self._context != context:
                return "obsolete"
            marker = response.get("team_view")
            marker = marker if isinstance(marker, Mapping) else {}
            parts = revision_parts(marker.get("revision"))
            negotiated = marker.get("protocol") == TEAM_PROTOCOL and parts is not None and marker.get("scope") == parts[0]
            team = response.get("team")
            new_revision = ""
            if negotiated and marker.get("state") == "unchanged" and "team" in response:
                self._snapshots.pop(request.project_ref, None)
                return "full_required"
            if isinstance(team, list):
                # A full legacy/downgraded/malformed-tag reply is still a full
                # replacement, but never grants conditional knowledge.
                if negotiated and marker.get("state") == "changed" and context is not None:
                    candidate = _Snapshot(str(marker["revision"]), team)
                    if not self._intact(candidate):
                        self._snapshots.pop(request.project_ref, None)
                        return "full_required"
                    new_revision = candidate.revision
            elif "team" not in response and negotiated and marker.get("state") == "unchanged":
                saved = self._snapshots.get(request.project_ref)
                if saved is None or not request.known_revision or marker["revision"] != request.known_revision or saved.revision != request.known_revision or not self._intact(saved):
                    self._snapshots.pop(request.project_ref, None)
                    return "full_required"
                team = saved.team
                new_revision = saved.revision
            else:
                self._snapshots.pop(request.project_ref, None)
                return "full_required"
            snapshot = deepcopy(team)
            try:
                materialize(snapshot)
            except BaseException:
                self._snapshots.pop(request.project_ref, None)
                raise
            if new_revision:
                # A defensive second copy keeps consumer mutation from
                # silently changing the token's retained full snapshot.
                self._snapshots[request.project_ref] = _Snapshot(new_revision, deepcopy(team))
            else:
                self._snapshots.pop(request.project_ref, None)
            return "unchanged" if marker.get("state") == "unchanged" and new_revision else "full"

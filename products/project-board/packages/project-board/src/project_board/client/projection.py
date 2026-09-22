from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from project_board.contract.projection_policy import (
    FORBIDDEN_KEYS,
    PLAN_PROJECTION_PAGE_LIMIT,
)

from .contested import tally
from .io import content_hash


PROJECTION_SCHEMA = "problem-board.project-projection.v1"
CURRENT_WORK_STATUSES = {"awaiting_operator", "in_progress", "running", "working"}
CANCELLED_WORK_STATUSES = {"canceled", "cancelled"}


def _assert_safe(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or normalized.endswith(("_token", "_secret", "_credential")):
                raise ValueError(f"unsafe project projection field: {'.'.join((*path, str(key)))}")
            _assert_safe(child, (*path, str(key)))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_safe(child, (*path, str(index)))


def _select_projection_page(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep the operational present when the public projection is bounded."""

    ranked = sorted(
        (dict(item) for item in items),
        key=lambda item: (
            str(item.get("updated_at") or item.get("started_at") or ""),
            int(item.get("ordinal") or 0),
            str(item.get("item_ref") or ""),
        ),
        reverse=True,
    )

    def lifecycle_rank(item: Mapping[str, Any]) -> int:
        status = str(item.get("status") or "").lower()
        if status in CURRENT_WORK_STATUSES:
            return 0
        if status in CANCELLED_WORK_STATUSES:
            return 2
        return 1

    # Python's sort is stable: lifecycle importance is primary while recency
    # remains the order within each group. Render the selected rows in authored
    # order so changing the projection policy does not shuffle the graph.
    ranked.sort(key=lifecycle_rank)
    selected = ranked[:PLAN_PROJECTION_PAGE_LIMIT]
    selected.sort(
        key=lambda item: (
            int(item.get("ordinal") or 0),
            str(item.get("item_ref") or ""),
        )
    )
    return selected


def build_projection(
    *,
    project: Mapping[str, Any],
    graph: Mapping[str, Any],
    workers: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    contested: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    dependencies: dict[str, list[str]] = {}
    for edge in graph.get("dependencies") or []:
        if not isinstance(edge, Mapping):
            continue
        item_ref = str(edge.get("item_ref") or "")
        depends_on = str(edge.get("depends_on_ref") or "")
        if item_ref and depends_on:
            dependencies.setdefault(item_ref, []).append(depends_on)

    def _projected(
        row: Mapping[str, Any], item_ref: str, ordinal: int
    ) -> dict[str, Any]:
        # Notes are summarised, never carried in full. A busy item would
        # otherwise grow the published document without bound, which is the
        # problem the paging and archiving work exists to solve. The count
        # and the newest note are enough for a list; the full ordered history
        # is read on demand when someone opens the item.
        notes = [note for note in (row.get("notes") or []) if isinstance(note, Mapping)]
        latest = notes[-1] if notes else None
        projected = {
            "item_ref": item_ref,
            "item_key": str(row.get("item_key") or row.get("item_id") or item_ref),
            "title": str(row.get("title") or ""),
            "status": str(row.get("status") or "draft"),
            "assignee": str(row.get("assignee") or ""),
            "depends_on": sorted(dependencies.get(item_ref, [])),
            "revision": int(row.get("revision") or 1),
            "ordinal": ordinal,
            "started_at": str(row.get("started_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "note_count": len(notes),
            "latest_note": (
                {
                    "note_ref": str(latest.get("note_ref") or ""),
                    "author_label": str(latest.get("author") or ""),
                    "author_kind": str(latest.get("author_kind") or ""),
                    "text": str(latest.get("text") or "")[:400],
                    "created_at": str(latest.get("created_at") or ""),
                }
                if latest is not None
                else None
            ),
        }
        # A working item that nobody is moving must not read as progress on the
        # one surface an operator actually looks at. Carried only when true, so
        # the projection does not grow a field per node for the normal case.
        if row.get("stalled"):
            projected.update(
                stalled=True,
                stalled_reason=str(row.get("stalled_reason") or ""),
                assignee_last_activity_at=str(row.get("assignee_last_activity_at") or ""),
                assignee_silent_seconds=row.get("assignee_silent_seconds"),
            )
        if str(row.get("status") or "") == "cancelled":
            projected.update(
                cancelled_at=str(row.get("cancelled_at") or ""),
                cancelled_by=str(row.get("cancelled_by") or ""),
                cancel_reason=str(row.get("cancel_reason") or ""),
            )
        return projected

    # Cancelled work leaves the active graph and stays in the record. It is
    # kept resolvable so a blocked dependent can name the cancelled item that
    # is blocking it rather than pointing at nothing.
    projected_items: list[dict[str, Any]] = []
    for ordinal, row in enumerate(graph.get("items") or []):
        if not isinstance(row, Mapping):
            continue
        item_ref = str(row.get("item_ref") or "")
        projected_items.append(_projected(row, item_ref, ordinal))

    first_page = _select_projection_page(projected_items)
    items: list[dict[str, Any]] = []
    cancelled_items: list[dict[str, Any]] = []
    for projected in first_page:
        if projected["status"] == "cancelled":
            cancelled_items.append(projected)
        else:
            items.append(projected)

    active_items = [
        item for item in projected_items if item.get("status") != "cancelled"
    ]
    cancelled_count = len(projected_items) - len(active_items)
    statuses = Counter(str(item.get("status") or "unknown") for item in active_items)
    worker_rows = [
        {
            "worker_name": str(row.get("worker_name") or ""),
            "runtime_kind": str(row.get("runtime_kind") or ""),
            "pool_status": str(row.get("pool_status") or "active"),
            "availability": str(row.get("availability") or "unknown"),
            "heartbeat_at": str(row.get("heartbeat_at") or ""),
            "host_id": str(row.get("host_id") or ""),
            "host_label": str(row.get("host_label") or ""),
            "host_kind": str(row.get("host_kind") or ""),
            "relay_id": str(row.get("relay_id") or ""),
        }
        for row in workers
        if isinstance(row, Mapping)
    ]
    latest_events = [
        {
            "event_ref": str(row.get("event_ref") or ""),
            "kind": str(row.get("kind") or ""),
            "summary": str(row.get("summary") or "")[:500],
            "worker_name": str(row.get("worker_name") or ""),
            "work_ref": str(row.get("work_ref") or ""),
            "created_at": str(row.get("created_at") or ""),
        }
        for row in list(events)[-25:]
        if isinstance(row, Mapping)
    ]
    # Counts only, with the calls left behind. The projection is a bounded
    # summary every board read carries, and the calls carry claims and evidence
    # that belong on a page someone opened deliberately. A count is enough to
    # show the matrix exists and who is in it.
    contested_tally = tally(contested)
    projection: dict[str, Any] = {
        "schema": PROJECTION_SCHEMA,
        "contested": {"tally": contested_tally, "call_count": len(list(contested))},
        "project_ref": str(project.get("project_ref") or ""),
        "title": str(project.get("title") or ""),
        "status": str(project.get("status") or "active"),
        "revision": int(project.get("revision") or 1),
        "graph": {
            "plan_ref": str(graph.get("plan_ref") or ""),
            "status": str(graph.get("status") or "draft"),
            "revision": int(graph.get("revision") or 0),
            "items": items,
            "cancelled_items": cancelled_items,
            "item_count": len(projected_items),
            "active_item_count": len(active_items),
            "cancelled_item_count": cancelled_count,
            "page_count": len(first_page),
            "has_more": len(projected_items) > len(first_page),
            "selection_policy": "current_then_recent",
            "status_counts": dict(sorted(statuses.items())),
        },
        "workers": worker_rows,
        "latest_events": latest_events,
        "updated_at": str(project.get("updated_at") or ""),
    }
    _assert_safe(projection)
    projection["content_hash"] = content_hash(projection)
    return projection


__all__ = [
    "FORBIDDEN_KEYS",
    "PLAN_PROJECTION_PAGE_LIMIT",
    "PROJECTION_SCHEMA",
    "build_projection",
]

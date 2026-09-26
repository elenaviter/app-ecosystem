"""Operation groups a service declares for a Card editor (W260).

A service catalog names, beside its operations, how they are grouped (for
Problem Board: Review, Work, Plan, People). Each operation carries ``group``;
the resource or namespace carries ``operation_groups``, a map of group key to
``{label, order}`` (a bare string is the label). Connection Hub's Card view
groups by it, so no client builds its own grouping. Presentation only: it
stays out of every descriptor digest, so regrouping never raises drift.
"""

from __future__ import annotations

from typing import Any, Mapping


def _text(value: Any) -> str:
    return str(value or "").strip()


def parse_operation_groups(raw: Any) -> tuple[dict[str, Any], ...]:
    """The declared groups in display order: by ``order``, then declaration order."""

    if not isinstance(raw, Mapping):
        return ()
    rows: list[tuple[float, int, dict[str, Any]]] = []
    for index, (key, value) in enumerate(raw.items()):
        name = _text(key)
        if not name:
            continue
        data = value if isinstance(value, Mapping) else {"label": value}
        try:
            order = float(data["order"]) if data.get("order") is not None else float(index)
        except (TypeError, ValueError):
            order = float(index)
        rows.append((order, index, {"group": name, "label": _text(data.get("label")) or name, "order": order}))
    return tuple(row for _order, _index, row in sorted(rows, key=lambda item: (item[0], item[1])))


GROUPING_KEYS = frozenset({"group", "operation_groups"})


def without_grouping(value: Any) -> Any:
    """``value`` with every grouping key removed, at any depth (for digests)."""

    if isinstance(value, Mapping):
        return {key: without_grouping(item) for key, item in value.items() if key not in GROUPING_KEYS}
    if isinstance(value, (list, tuple)):
        return [without_grouping(item) for item in value]
    return value

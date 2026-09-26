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


def _drop(entry: Any, key: str) -> Any:
    if not isinstance(entry, Mapping):
        return entry
    return {name: value for name, value in entry.items() if name != key}


def without_grouping(named_services: Any) -> Any:
    """A ``named_services`` tree without its grouping, for a descriptor digest.

    Only where the schema puts grouping: ``operation_groups`` on a namespace,
    ``group`` on a tool entry and on an operation entry. A tool, operation or
    namespace that is itself named ``group`` is content and stays.
    """

    if not isinstance(named_services, Mapping):
        return named_services
    namespaces = named_services.get("namespaces")
    if not isinstance(namespaces, Mapping):
        return dict(named_services)
    stripped: dict[str, Any] = {}
    for name, namespace in namespaces.items():
        namespace = _drop(namespace, "operation_groups")
        tools = namespace.get("tools") if isinstance(namespace, Mapping) else None
        if isinstance(tools, Mapping):
            kept_tools: dict[str, Any] = {}
            for tool_name, tool in tools.items():
                tool = _drop(tool, "group")
                operations = tool.get("operations") if isinstance(tool, Mapping) else None
                if isinstance(operations, Mapping):
                    tool = {
                        **tool,
                        "operations": {op: _drop(policy, "group") for op, policy in operations.items()},
                    }
                kept_tools[tool_name] = tool
            namespace = {**namespace, "tools": kept_tools}
        stripped[name] = namespace
    return {**named_services, "namespaces": stripped}

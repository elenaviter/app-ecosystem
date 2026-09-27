"""Operation groups a service declares for a Card editor (W260).

A service catalog names, beside its operations, how they are grouped (for
Problem Board: Review, Work, Plan, People). Each operation carries ``group``;
the resource or namespace carries ``operation_groups``, a map of group key to
``{label, order}`` (a bare string is the label). Connection Hub's Card view
groups by it, so no client builds its own grouping. Presentation only: it
stays out of every descriptor digest, so regrouping never raises drift.

``person_card: false`` on an operation (W360) is presentation of the same
kind: the service decides that operation for a person by role alone, so a
project person's Control Card does not offer it. It stays out of the digest
too, and an operation without it is offered as before.
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


def _drop(entry: Any, *keys: str) -> Any:
    if not isinstance(entry, Mapping):
        return entry
    return {name: value for name, value in entry.items() if name not in keys}


# What a tool or operation entry says about presentation, never authority.
_PRESENTATION_KEYS = ("group", "person_card")


def offered_on_person_card(entry: Any) -> bool:
    """False only when the entry says ``person_card: false`` (W360)."""

    if not isinstance(entry, Mapping) or "person_card" not in entry:
        return True
    value = entry.get("person_card")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "n", "off"}


def without_grouping(named_services: Any) -> Any:
    """A ``named_services`` tree without its presentation, for a descriptor digest.

    Only where the schema puts it: ``operation_groups`` on a namespace,
    ``group`` and ``person_card`` on a tool entry and on an operation entry. A
    tool, operation or namespace that is itself named ``group`` is content and
    stays.
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
                tool = _drop(tool, *_PRESENTATION_KEYS)
                operations = tool.get("operations") if isinstance(tool, Mapping) else None
                if isinstance(operations, Mapping):
                    tool = {
                        **tool,
                        "operations": {op: _drop(policy, *_PRESENTATION_KEYS) for op, policy in operations.items()},
                    }
                kept_tools[tool_name] = tool
            namespace = {**namespace, "tools": kept_tools}
        stripped[name] = namespace
    return {**named_services, "namespaces": stripped}

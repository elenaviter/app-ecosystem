# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Stable MCP-safe names and their exact reverse routing index."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable

from connection_hub.delegated_gateway.models import (
    ACCESS_DESCRIBE_TOOL,
    GatewayContractError,
    GatewayToolRoute,
)

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_QUALIFIED_NAME = re.compile(
    r"^ch_[a-z0-9_]{1,20}_[a-f0-9]{16}__[A-Za-z0-9_-]{1,40}_[a-f0-9]{16}$"
)


def _default_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str, *, limit: int) -> str:
    slug = _UNSAFE_NAME.sub("_", str(value or "").strip()).strip("_-")
    return (slug or "operation")[:limit]


def qualified_tool_name(
    route: GatewayToolRoute,
    *,
    hash_text: Callable[[str], str] = _default_hash,
) -> str:
    """Name a route from stable authority identity, never a label or origin."""

    resource_hash = hash_text(route.resource_id)
    route_hash = hash_text("\x1f".join(route.identity))
    if len(resource_hash) < 16 or len(route_hash) < 16:
        raise GatewayContractError("qualified_name_hash_invalid")
    kind = _slug(route.resource_kind, limit=20).lower()
    operation = _slug(route.operation, limit=40)
    name = f"ch_{kind}_{resource_hash[:16]}__{operation}_{route_hash[:16]}"
    if len(name) > 128 or not _QUALIFIED_NAME.fullmatch(name):
        raise GatewayContractError("qualified_tool_name_invalid")
    if name == ACCESS_DESCRIBE_TOOL:
        raise GatewayContractError("qualified_tool_name_reserved")
    return name


def is_qualified_tool_name(value: str) -> bool:
    return bool(_QUALIFIED_NAME.fullmatch(str(value or "").strip()))


class QualifiedToolNameIndex:
    """Bidirectional request-local index with collision rejection."""

    def __init__(
        self,
        routes: Iterable[GatewayToolRoute] = (),
        *,
        hash_text: Callable[[str], str] = _default_hash,
    ) -> None:
        self._hash_text = hash_text
        self._by_name: dict[str, GatewayToolRoute] = {}
        self._by_route: dict[tuple[str, str, str], str] = {}
        for route in routes:
            self.add(route)

    def add(self, route: GatewayToolRoute) -> str:
        name = qualified_tool_name(route, hash_text=self._hash_text)
        existing_route = self._by_name.get(name)
        if existing_route is not None and existing_route.identity != route.identity:
            raise GatewayContractError("qualified_tool_name_collision")
        existing_name = self._by_route.get(route.identity)
        if existing_name is not None and existing_name != name:
            raise GatewayContractError("qualified_tool_route_collision")
        self._by_name[name] = route
        self._by_route[route.identity] = name
        return name

    def resolve(self, name: str) -> GatewayToolRoute | None:
        return self._by_name.get(str(name or "").strip())

    def name_for(self, route: GatewayToolRoute) -> str:
        name = self._by_route.get(route.identity)
        if name is None:
            raise GatewayContractError("qualified_tool_route_not_indexed")
        return name

    def items(self) -> tuple[tuple[str, GatewayToolRoute], ...]:
        return tuple(sorted(self._by_name.items()))


__all__ = [
    "QualifiedToolNameIndex",
    "is_qualified_tool_name",
    "qualified_tool_name",
]

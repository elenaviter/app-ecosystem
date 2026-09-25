"""Canonical identity for the repositories that make up a Project Board client."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


APP_ECOSYSTEM_COMPONENT = "app_ecosystem"
KDCUBE_COMPONENT = "kdcube"

APP_ECOSYSTEM_SOURCE_PATHS = (
    "products/project-board/packages/project-board",
    "packages/app-foundation",
    "packages/service-foundation",
    "products/connection-hub/packages/connection-hub",
    "products/connection-hub/packages/connection-hub-cli",
)
KDCUBE_SOURCE_PATHS = (
    "app/ai-app/src/kdcube-ai-app/kdcube_cli",
)
SOURCE_PATHS_BY_COMPONENT = {
    APP_ECOSYSTEM_COMPONENT: APP_ECOSYSTEM_SOURCE_PATHS,
    KDCUBE_COMPONENT: KDCUBE_SOURCE_PATHS,
}
CLIENT_SOURCE_PATHS = tuple(
    path
    for component in (APP_ECOSYSTEM_COMPONENT, KDCUBE_COMPONENT)
    for path in SOURCE_PATHS_BY_COMPONENT[component]
)
SOURCE_IMPORTS_BY_PATH = {
    "products/project-board/packages/project-board": "project_board",
    "packages/app-foundation": "app_foundation",
    "packages/service-foundation": "service_foundation",
    "products/connection-hub/packages/connection-hub": "connection_hub",
    "products/connection-hub/packages/connection-hub-cli": "connection_hub_cli",
    "app/ai-app/src/kdcube-ai-app/kdcube_cli": "kdcube_cli",
}
CLIENT_SOURCE_IMPORTS = tuple(
    SOURCE_IMPORTS_BY_PATH[path] for path in CLIENT_SOURCE_PATHS
)
IDENTITY_SCHEMA = "project-board.client-source-identity.v1"


@dataclass(frozen=True, slots=True)
class SourceComponent:
    """One repository commit and the package trees consumed from it."""

    name: str
    commit: str
    subtrees: dict[str, str]
    repository: str = ""

    def record(self, *, include_repository: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "commit": self.commit,
            "subtrees": dict(sorted(self.subtrees.items())),
        }
        if include_repository and self.repository:
            value["repository"] = self.repository
        return value


def _full_hex(value: Any, *, lengths: tuple[int, ...], label: str) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) not in lengths or any(
        character not in "0123456789abcdef" for character in clean
    ):
        expected = " or ".join(str(length) for length in lengths)
        raise ValueError(
            f"{label} must be {expected} lowercase hexadecimal characters"
        )
    return clean


def component_from_record(value: Mapping[str, Any]) -> SourceComponent:
    """Validate one persisted component against the package ownership map."""

    name = str(value.get("name") or "").strip()
    required_paths = SOURCE_PATHS_BY_COMPONENT.get(name)
    if required_paths is None:
        raise ValueError(f"unknown source component: {name or '<empty>'}")
    raw_subtrees = value.get("subtrees")
    if not isinstance(raw_subtrees, Mapping):
        raise ValueError(f"source component {name} has no subtree mapping")
    subtrees = {
        str(path): _full_hex(tree, lengths=(40, 64), label=f"tree id for {path}")
        for path, tree in raw_subtrees.items()
    }
    if set(subtrees) != set(required_paths):
        raise ValueError(
            f"source component {name} must name exactly: {', '.join(required_paths)}"
        )
    return SourceComponent(
        name=name,
        commit=_full_hex(
            value.get("commit"), lengths=(40,), label=f"{name} commit"
        ),
        subtrees={path: subtrees[path] for path in required_paths},
        repository=str(value.get("repository") or ""),
    )


def normalise_components(
    values: Iterable[SourceComponent | Mapping[str, Any]],
    *,
    require_complete: bool = True,
) -> tuple[SourceComponent, ...]:
    """Return unique components in canonical name order."""

    components: dict[str, SourceComponent] = {}
    for raw in values:
        value = raw.record() if isinstance(raw, SourceComponent) else raw
        if not isinstance(value, Mapping):
            raise ValueError("each source component must be an object")
        component = component_from_record(value)
        if component.name in components:
            raise ValueError(f"duplicate source component: {component.name}")
        components[component.name] = component
    required = set(SOURCE_PATHS_BY_COMPONENT)
    if require_complete and set(components) != required:
        raise ValueError(
            "client source components must name exactly: "
            + ", ".join(sorted(required))
        )
    return tuple(components[name] for name in sorted(components))


def component_records(
    components: Iterable[SourceComponent | Mapping[str, Any]],
    *,
    include_repository: bool = True,
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    return [
        component.record(include_repository=include_repository)
        for component in normalise_components(
            components, require_complete=require_complete
        )
    ]


def release_id_for_components(
    components: Iterable[SourceComponent | Mapping[str, Any]],
) -> str:
    """A path-independent digest of every repository commit and package tree."""

    identity = {
        "schema": IDENTITY_SCHEMA,
        "components": component_records(
            components,
            include_repository=False,
            require_complete=True,
        ),
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def validate_release_id(value: Any) -> str:
    return _full_hex(value, lengths=(64,), label="client source release id")


def component_named(
    components: Iterable[SourceComponent], name: str
) -> SourceComponent:
    for component in components:
        if component.name == name:
            return component
    raise KeyError(name)


__all__ = [
    "APP_ECOSYSTEM_COMPONENT",
    "APP_ECOSYSTEM_SOURCE_PATHS",
    "CLIENT_SOURCE_IMPORTS",
    "CLIENT_SOURCE_PATHS",
    "KDCUBE_COMPONENT",
    "KDCUBE_SOURCE_PATHS",
    "SOURCE_PATHS_BY_COMPONENT",
    "SOURCE_IMPORTS_BY_PATH",
    "SourceComponent",
    "component_from_record",
    "component_named",
    "component_records",
    "normalise_components",
    "release_id_for_components",
    "validate_release_id",
]

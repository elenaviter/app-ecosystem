"""Canonical identity for the repositories that make up a Project Board client."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


APP_ECOSYSTEM_COMPONENT = "app_ecosystem"
KDCUBE_COMPONENT = "kdcube"

# The client is App Ecosystem only (W322 Step 1): pb depends on
# connection-hub[client], so neither the Connection Hub command line nor KDCube's
# is part of it. The earlier identity (v1, W255) named both and stays readable,
# so a host can still show and roll back to a release built before the change.
APP_ECOSYSTEM_SOURCE_PATHS = (
    "products/project-board/packages/project-board",
    "packages/app-foundation",
    "packages/service-foundation",
    "products/connection-hub/packages/connection-hub",
)
APP_ECOSYSTEM_SOURCE_PATHS_V1 = (
    *APP_ECOSYSTEM_SOURCE_PATHS,
    "products/connection-hub/packages/connection-hub-cli",
)
KDCUBE_SOURCE_PATHS = (
    "app/ai-app/src/kdcube-ai-app/kdcube_cli",
)
IDENTITY_SCHEMA_V1 = "project-board.client-source-identity.v1"
IDENTITY_SCHEMA = "project-board.client-source-identity.v2"
SOURCE_PATHS_BY_SCHEMA = {
    IDENTITY_SCHEMA_V1: {
        APP_ECOSYSTEM_COMPONENT: APP_ECOSYSTEM_SOURCE_PATHS_V1,
        KDCUBE_COMPONENT: KDCUBE_SOURCE_PATHS,
    },
    IDENTITY_SCHEMA: {
        APP_ECOSYSTEM_COMPONENT: APP_ECOSYSTEM_SOURCE_PATHS,
    },
}
# What a new release is built from.
SOURCE_PATHS_BY_COMPONENT = SOURCE_PATHS_BY_SCHEMA[IDENTITY_SCHEMA]
CLIENT_COMPONENTS = tuple(SOURCE_PATHS_BY_COMPONENT)
CLIENT_SOURCE_PATHS = tuple(
    path for component in CLIENT_COMPONENTS for path in SOURCE_PATHS_BY_COMPONENT[component]
)
CLIENT_SOURCE_PATHS_V1 = (*APP_ECOSYSTEM_SOURCE_PATHS_V1, *KDCUBE_SOURCE_PATHS)
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


def identity_schema_for(names: Iterable[str]) -> str:
    """The identity a set of component names belongs to.

    A KDCube component exists only in the v1 identity; every other set is the
    current one.
    """

    return IDENTITY_SCHEMA_V1 if KDCUBE_COMPONENT in set(names) else IDENTITY_SCHEMA


def client_source_paths_for(components: Iterable["SourceComponent"]) -> tuple[str, ...]:
    """The package paths a release with these components exports, in order."""

    schema = identity_schema_for(component.name for component in components)
    return CLIENT_SOURCE_PATHS_V1 if schema == IDENTITY_SCHEMA_V1 else CLIENT_SOURCE_PATHS


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


def component_from_record(
    value: Mapping[str, Any], *, schema: str = IDENTITY_SCHEMA
) -> SourceComponent:
    """Validate one persisted component against its identity's package map."""

    name = str(value.get("name") or "").strip()
    required_paths = SOURCE_PATHS_BY_SCHEMA[schema].get(name)
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

    records: list[Mapping[str, Any]] = []
    for raw in values:
        value = raw.record() if isinstance(raw, SourceComponent) else raw
        if not isinstance(value, Mapping):
            raise ValueError("each source component must be an object")
        records.append(value)
    schema = identity_schema_for(str(value.get("name") or "").strip() for value in records)
    components: dict[str, SourceComponent] = {}
    for value in records:
        component = component_from_record(value, schema=schema)
        if component.name in components:
            raise ValueError(f"duplicate source component: {component.name}")
        components[component.name] = component
    required = set(SOURCE_PATHS_BY_SCHEMA[schema])
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

    records = component_records(
        components,
        include_repository=False,
        require_complete=True,
    )
    identity = {
        # A v1 release keeps the id it was built with.
        "schema": identity_schema_for(record["name"] for record in records),
        "components": records,
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
    "APP_ECOSYSTEM_SOURCE_PATHS_V1",
    "CLIENT_COMPONENTS",
    "CLIENT_SOURCE_IMPORTS",
    "CLIENT_SOURCE_PATHS",
    "CLIENT_SOURCE_PATHS_V1",
    "IDENTITY_SCHEMA",
    "IDENTITY_SCHEMA_V1",
    "KDCUBE_COMPONENT",
    "KDCUBE_SOURCE_PATHS",
    "SOURCE_PATHS_BY_COMPONENT",
    "SOURCE_PATHS_BY_SCHEMA",
    "client_source_paths_for",
    "identity_schema_for",
    "SOURCE_IMPORTS_BY_PATH",
    "SourceComponent",
    "component_from_record",
    "component_named",
    "component_records",
    "normalise_components",
    "release_id_for_components",
    "validate_release_id",
]

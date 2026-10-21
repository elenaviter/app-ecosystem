# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A project's External MCP switch over its members' own connectors (W918).

A person's or agent's custom MCP connectors are their own resources, one per
connector (``urn:connection-hub:remote-mcp:<connector-id>``), resolved under
the Card's owner. A project cannot list them: "the project cannot know
beforehand which exactly services will be connected by each user" (operator,
2026-10-21). So the project Control P does not carry connector grants; it
carries one switch, and a caller's own connectors are decided by that switch
instead of by per-resource AND.

The switch is read only from a positively validated project chain: an agent
Card bound directly to its project's P, or a person's My Card under their
project Control C under that P. On every other chain nothing here applies and
connectors fold like any other resource.

- P ``external_mcp_family`` is ``on`` or ``off``; absent or invalid is off.
- A C may say ``off`` (narrow) and never turns on what P has off; absent on C
  inherits P.
- On: the caller's own connectors pass with their own grants, and their tools
  are bounded by the deployment's project family (allowed tools and limits).
- Off: the caller's connectors are dropped. No Control ever contributes one.

The family limits are deployment-owned
(``connections.delegated_credentials.project_resource_families``) and
configured once at startup; an unconfigured, invalid or unknown family closes
the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any, Iterable, Mapping

from connection_hub.delegated_credentials.cards.model import CardAuthority, authority_is_credentialless
from connection_hub.delegated_credentials.controls.model import ControlCardError, control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.snapshot import control_snapshot_is_exact
from connection_hub.remote_mcp.models import connector_id_from_resource

EXTERNAL_MCP_FAMILY_PROPERTY = "external_mcp_family"
FAMILY_ON = "on"
FAMILY_OFF = "off"
FAMILY_VALUES = frozenset({FAMILY_ON, FAMILY_OFF})
EXTERNAL_MCP_FAMILY_ID = "user_external_mcp"
# The project Control's issuer kind (as controls.project_person_composition).
_PROJECT_CONTROL_ISSUER_KIND = "application"


class ProjectFamilyConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProjectResourceFamily:
    """One deployment-declared family a project switch may admit."""

    family_id: str
    resource_patterns: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    max_resources: int
    max_tools_per_resource: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProjectResourceFamily":
        if not isinstance(raw, Mapping):
            raise ProjectFamilyConfigError("project_resource_family_invalid")
        family_id = raw.get("id")
        patterns = raw.get("resource_patterns")
        tools = raw.get("allowed_tools")
        kinds = raw.get("resource_kinds")
        if not isinstance(family_id, str) or not family_id.strip():
            raise ProjectFamilyConfigError("project_resource_family_id_invalid")
        if kinds is not None and (not isinstance(kinds, list) or kinds != ["remote_mcp"]):
            raise ProjectFamilyConfigError("project_resource_family_kind_unsupported")
        if (not isinstance(patterns, list) or not patterns
                or not all(isinstance(p, str) and p.startswith("urn:connection-hub:remote-mcp:") for p in patterns)):
            raise ProjectFamilyConfigError("project_resource_family_patterns_invalid")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise ProjectFamilyConfigError("project_resource_family_tools_invalid")
        limits = []
        for name in ("max_resources", "max_tools_per_resource"):
            value = raw.get(name)
            if type(value) is not int or value < 1:
                raise ProjectFamilyConfigError(f"project_resource_family_{name}_invalid")
            limits.append(value)
        return cls(family_id.strip(), tuple(patterns), tuple(tools), limits[0], limits[1])

    def matches(self, resource: str) -> bool:
        """The Hub's own connector predicate AND a declared pattern; never a label or URL."""
        return bool(connector_id_from_resource(resource)) and any(
            fnmatchcase(resource, pattern) for pattern in self.resource_patterns)


_FAMILIES: dict[str, ProjectResourceFamily] = {}


def configure_project_resource_families(entries: Iterable[Mapping[str, Any]] | None) -> None:
    """Load the deployment's declaration. Any invalid entry leaves no family configured."""
    _FAMILIES.clear()
    if entries is None:
        return
    parsed: dict[str, ProjectResourceFamily] = {}
    try:
        for raw in entries:
            family = ProjectResourceFamily.from_mapping(raw)
            if family.family_id in parsed:
                raise ProjectFamilyConfigError("project_resource_family_duplicate")
            parsed[family.family_id] = family
    except (ProjectFamilyConfigError, TypeError):
        return
    _FAMILIES.update(parsed)


def configured_family(family_id: str = EXTERNAL_MCP_FAMILY_ID) -> ProjectResourceFamily | None:
    return _FAMILIES.get(family_id)


def validate_family_property(properties: Mapping[str, Any]) -> None:
    """Card codec check: the switch is ``on`` or ``off`` when present."""
    if EXTERNAL_MCP_FAMILY_PROPERTY in properties and properties[EXTERNAL_MCP_FAMILY_PROPERTY] not in FAMILY_VALUES:
        raise ValueError("control_card_external_mcp_family_invalid")


# A project's own Control is an application Control issued for a Problem Board project ref; any
# other application Control (another app's ref) keeps the ordinary fold.
_PROJECT_REF_PREFIX = "work:project:"


def _is_project_control(control: CardAuthority, project_ref: str) -> bool:
    if control.issuer_kind != _PROJECT_CONTROL_ISSUER_KIND or str(control.issuer_ref or "") != project_ref:
        return False
    if not project_ref.startswith(_PROJECT_REF_PREFIX) or len(project_ref) <= len(_PROJECT_REF_PREFIX):
        return False
    if not authority_is_credentialless(control) or not control_snapshot_is_exact(control):
        return False
    if control.control_card is not None:
        return False
    try:
        expected = control_card_id_for_issuer(_PROJECT_CONTROL_ISSUER_KIND, project_ref,
                                              grantor_subject=control.grantor_subject)
    except ControlCardError:
        return False
    return control.access_id == expected


def _binds_project_control(child: CardAuthority, control: CardAuthority) -> str:
    """The project ref when ``child`` is bound to exactly that project's P, else ''."""
    binding = child.control_card
    if binding is None or binding.issuer_kind != _PROJECT_CONTROL_ISSUER_KIND:
        return ""
    project_ref = str(binding.issuer_ref or "").strip()
    # An empty holder means P's own holder owns this Card (as attach writes it and the resolver reads it).
    holder = str(binding.holder_subject or "").strip() or str(child.grantor_subject or "").strip()
    if not project_ref or binding.control_id != control.access_id or holder != control.grantor_subject:
        return ""
    return project_ref if _is_project_control(control, project_ref) else ""


def project_family_gate(card: CardAuthority, dependencies: tuple[CardAuthority, ...]) -> bool | None:
    """None off a validated project chain; otherwise whether the switch admits connectors.

    Validated chains: agent -> P, and My -> C -> P (C a project-person Control
    bound to P). Edge checks of the full hierarchy still run separately.
    """
    from connection_hub.delegated_credentials.controls.project_person_composition import (
        project_control_parent, project_held_control,
    )

    if len(dependencies) == 1:
        if not _binds_project_control(card, dependencies[0]):
            return None
        chain_controls: tuple[CardAuthority, ...] = ()
        project = dependencies[0]
    elif len(dependencies) == 2:
        person_control, project = dependencies
        if project_held_control(card) is None or project_control_parent(person_control) is None:
            return None
        if not _binds_project_control(person_control, project):
            return None
        chain_controls = (person_control,)
    else:
        return None
    if dict(project.properties or {}).get(EXTERNAL_MCP_FAMILY_PROPERTY) != FAMILY_ON:
        return False
    for control in chain_controls:
        value = dict(control.properties or {}).get(EXTERNAL_MCP_FAMILY_PROPERTY)
        if value is not None and value != FAMILY_ON:
            return False
    return True


def _is_connector(resource: str) -> bool:
    return bool(connector_id_from_resource(resource))


def strip_connectors(card: CardAuthority) -> CardAuthority:
    """The Card without any connector authority or evidence (grants, tools, acceptance).

    On a project chain no Control's connector ever folds, and no Control's
    acceptance evidence can stand in for the caller's own.
    """
    acceptance = dict(card.resource_acceptance or {})
    if not any(_is_connector(r) for r in (*card.resource_grants, *card.resource_operations, *acceptance)):
        return card
    return replace(
        card,
        resource_grants={r: g for r, g in card.resource_grants.items() if not _is_connector(r)},
        resource_operations={r: o for r, o in card.resource_operations.items() if not _is_connector(r)},
        resource_acceptance={r: a for r, a in acceptance.items() if not _is_connector(r)},
    )


def split_family_resources(card: CardAuthority, family: ProjectResourceFamily | None
                           ) -> tuple[CardAuthority, dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The card without ANY connector resource, and the caller's connectors the family can admit.

    Every connector leaves the fold, whether or not the family matches it: a
    connector outside the family is dropped, never folded by AND or OR.
    """
    connectors = sorted(r for r in card.resource_grants if _is_connector(r))
    admissible = [r for r in connectors if family is not None and family.matches(r)]
    grants = {r: tuple(card.resource_grants.get(r, ())) for r in admissible}
    operations = {r: tuple(card.resource_operations.get(r, ())) for r in admissible}
    return strip_connectors(card), grants, operations


def admitted_family_resources(family: ProjectResourceFamily, grants: Mapping[str, tuple[str, ...]],
                              operations: Mapping[str, tuple[str, ...]]
                              ) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The caller's own connectors under the family ceiling; over a limit refuses."""
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch

    if len(grants) > family.max_resources:
        raise ControlCardMismatch("project_resource_family_resource_limit")
    allowed = set(family.allowed_tools)
    admitted_grants: dict[str, tuple[str, ...]] = {}
    admitted_operations: dict[str, tuple[str, ...]] = {}
    for resource, resource_grants in grants.items():
        selected = tuple(operations.get(resource, ()))
        bounded = selected if "*" in allowed else tuple(op for op in selected if op in allowed)
        if len(set(bounded)) > family.max_tools_per_resource:
            raise ControlCardMismatch("project_resource_family_tool_limit")
        admitted_grants[resource] = tuple(resource_grants)
        admitted_operations[resource] = tuple(sorted(set(bounded)))
    return admitted_grants, admitted_operations


__all__ = [
    "EXTERNAL_MCP_FAMILY_ID",
    "EXTERNAL_MCP_FAMILY_PROPERTY",
    "FAMILY_OFF",
    "FAMILY_ON",
    "ProjectFamilyConfigError",
    "ProjectResourceFamily",
    "admitted_family_resources",
    "configure_project_resource_families",
    "configured_family",
    "project_family_gate",
    "split_family_resources",
    "strip_connectors",
    "validate_family_property",
]

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
(``connections.delegated_credentials.project_resource_families``). They are an
immutable ``ProjectFamilyPolicy`` value that each owning service receives from
its own descriptor read and passes to every composition explicitly; there is
no module state, so one tenant's or request's declaration can never reach
another's composition. No policy, an invalid declaration or an unknown family
closes the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any, Mapping

from connection_hub.delegated_credentials.cards.model import CardAuthority, authority_is_credentialless
from connection_hub.delegated_credentials.controls.model import ControlCardError, control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.snapshot import control_snapshot_is_exact
from connection_hub.delegated_credentials.controls.external_mcp_switch import (
    A2A_FAMILY_PROPERTY, EXTERNAL_MCP_FAMILY_PROPERTY, FAMILY_OFF, FAMILY_ON, FAMILY_VALUES, validate_family_property,
)
from connection_hub.remote_a2a.models import (
    RESOURCE_PREFIX as REMOTE_A2A_PREFIX, connector_id_from_resource as a2a_connector_id_from_resource,
)
from connection_hub.remote_mcp.models import RESOURCE_PREFIX as REMOTE_MCP_PREFIX, connector_id_from_resource

EXTERNAL_MCP_FAMILY_ID = "user_external_mcp"
EXTERNAL_A2A_FAMILY_ID = "user_external_a2a"

# The closed set of the caller's own connector kinds a project switch decides
# (W937 adds remote A2A next to W918's remote MCP): the resource kind, its URN
# prefix, the Hub's own connector predicate, and the project Control property
# that switches it. Each kind has its own switch; neither ever folds.
FAMILY_KINDS = {
    "remote_mcp": (REMOTE_MCP_PREFIX, connector_id_from_resource, EXTERNAL_MCP_FAMILY_PROPERTY),
    "remote_a2a": (REMOTE_A2A_PREFIX, a2a_connector_id_from_resource, A2A_FAMILY_PROPERTY),
}
FAMILY_SWITCHES = (
    ("remote_mcp", EXTERNAL_MCP_FAMILY_ID, EXTERNAL_MCP_FAMILY_PROPERTY),
    ("remote_a2a", EXTERNAL_A2A_FAMILY_ID, A2A_FAMILY_PROPERTY),
)
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
    resource_kind: str = "remote_mcp"

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
        if kinds is None:
            kind = "remote_mcp"
        elif isinstance(kinds, list) and len(kinds) == 1 and kinds[0] in FAMILY_KINDS:
            kind = kinds[0]
        else:
            raise ProjectFamilyConfigError("project_resource_family_kind_unsupported")
        prefix = FAMILY_KINDS[kind][0]
        if (not isinstance(patterns, list) or not patterns
                or not all(isinstance(p, str) and p.startswith(prefix) for p in patterns)):
            raise ProjectFamilyConfigError("project_resource_family_patterns_invalid")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise ProjectFamilyConfigError("project_resource_family_tools_invalid")
        limits = []
        for name in ("max_resources", "max_tools_per_resource"):
            value = raw.get(name)
            if type(value) is not int or value < 1:
                raise ProjectFamilyConfigError(f"project_resource_family_{name}_invalid")
            limits.append(value)
        return cls(family_id.strip(), tuple(patterns), tuple(tools), limits[0], limits[1], kind)

    def matches(self, resource: str) -> bool:
        """The Hub's own connector predicate AND a declared pattern; never a label or URL."""
        predicate = FAMILY_KINDS[self.resource_kind][1]
        return bool(predicate(resource)) and any(
            fnmatchcase(resource, pattern) for pattern in self.resource_patterns)

    def limits(self) -> dict[str, Any]:
        """The family's ceiling in the shape the shared family algebra reads."""
        return {"allowed_tools": list(self.allowed_tools), "max_resources": self.max_resources,
                "max_tools_per_resource": self.max_tools_per_resource}


@dataclass(frozen=True)
class ProjectFamilyPolicy:
    """One deployment's validated declaration, as an immutable value."""

    families: tuple[ProjectResourceFamily, ...] = ()

    @classmethod
    def from_declaration(cls, entries: Any) -> "ProjectFamilyPolicy":
        """The declaration's families; anything invalid gives the empty (closed) policy."""
        if not isinstance(entries, (list, tuple)):
            return cls()
        parsed: dict[str, ProjectResourceFamily] = {}
        try:
            for raw in entries:
                family = ProjectResourceFamily.from_mapping(raw)
                if family.family_id in parsed:
                    raise ProjectFamilyConfigError("project_resource_family_duplicate")
                parsed[family.family_id] = family
        except (ProjectFamilyConfigError, TypeError):
            return cls()
        return cls(tuple(parsed[key] for key in sorted(parsed)))

    @classmethod
    def from_connections(cls, connections: Any) -> "ProjectFamilyPolicy":
        """Read ``delegated_credentials.project_resource_families`` from a ``connections`` mapping."""
        delegated = connections.get("delegated_credentials") if isinstance(connections, Mapping) else None
        return cls.from_declaration(
            delegated.get("project_resource_families") if isinstance(delegated, Mapping) else None)

    def family(self, family_id: str = EXTERNAL_MCP_FAMILY_ID) -> ProjectResourceFamily | None:
        return next((family for family in self.families if family.family_id == family_id), None)

    def switched_family(self, family_id: str, resource_kind: str) -> ProjectResourceFamily | None:
        """The family with this id, only if it is declared for this connector kind."""
        family = self.family(family_id)
        return family if family is not None and family.resource_kind == resource_kind else None


NO_PROJECT_FAMILIES = ProjectFamilyPolicy()


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


def project_family_gate(card: CardAuthority, dependencies: tuple[CardAuthority, ...], *,
                        switch_property: str = EXTERNAL_MCP_FAMILY_PROPERTY) -> bool | None:
    """None off a validated project chain; otherwise whether the switch admits connectors.

    ``switch_property`` names the project Control property of one family's
    switch (``external_mcp_family`` by default, ``a2a_family`` for A2A).

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
    if dict(project.properties or {}).get(switch_property) != FAMILY_ON:
        return False
    for control in chain_controls:
        value = dict(control.properties or {}).get(switch_property)
        if value is not None and value != FAMILY_ON:
            return False
    return True


def _is_connector(resource: str) -> bool:
    """Any of the caller's own connector kinds (remote MCP or remote A2A)."""
    return any(bool(predicate(resource)) for _prefix, predicate, _property in FAMILY_KINDS.values())


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
    """The caller's own connectors under the family ceiling; over a limit refuses.

    The same bounded-tools and limit algebra as descriptor families
    (``effective.bounded_family_authority``), with this family's own codes.
    """
    from connection_hub.delegated_credentials.controls.effective import bounded_family_authority

    limits = family.limits()
    return bounded_family_authority(
        grants, operations, {resource: (family.family_id, limits) for resource in grants},
        {family.family_id: limits},
        resource_limit_reason="project_resource_family_resource_limit",
        tool_limit_reason="project_resource_family_tool_limit",
    )


__all__ = [
    "A2A_FAMILY_PROPERTY",
    "EXTERNAL_A2A_FAMILY_ID",
    "EXTERNAL_MCP_FAMILY_ID",
    "FAMILY_KINDS",
    "FAMILY_SWITCHES",
    "EXTERNAL_MCP_FAMILY_PROPERTY",
    "FAMILY_OFF",
    "FAMILY_ON",
    "NO_PROJECT_FAMILIES",
    "ProjectFamilyConfigError",
    "ProjectFamilyPolicy",
    "ProjectResourceFamily",
    "admitted_family_resources",
    "project_family_gate",
    "split_family_resources",
    "strip_connectors",
    "validate_family_property",
]

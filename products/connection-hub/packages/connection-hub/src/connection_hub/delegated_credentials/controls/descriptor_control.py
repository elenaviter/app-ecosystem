# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""One descriptor Control per agent, held by the application.

The Control is shared by every user of the agent: one Control per (tenant,
project, application, agent) is stored under a synthetic application
subject, the way a project's Cards live under ``project-authority:<digest>``. The subject is a holder only: it has no
credential and never signs in.

The holder and the Control id are computed from the binding's ``issuer_ref``,
never taken from a request or a stored binding alone. An Agent Card reaches
its shared Control only through the exact edge below; a binding that names
any other holder or Control id is not a descriptor edge, and the descriptor's
resource families (the user's own MCP and A2A connectors) admit nothing on it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from connection_hub.delegated_credentials.agent_capability_control import AGENT_DESCRIPTOR_ISSUER_KIND
from connection_hub.delegated_credentials.application_resources import ApplicationResource, ApplicationResourceError
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.controls.model import ControlCardError, control_card_id_for_issuer

APPLICATION_AUTHORITY_PREFIX = "application-authority:"


def application_authority_subject(*, tenant: str, project: str, application: str) -> str:
    """The stable synthetic holder of one application's descriptor Controls."""

    parsed = ApplicationResource(tenant=tenant, project=project, application=application, agent="holder")
    if parsed.application == "*":
        raise ApplicationResourceError("application_resource_not_exact")
    digest = hashlib.sha256(
        f"{parsed.tenant}\0{parsed.project}\0{parsed.application}".encode("utf-8")
    ).hexdigest()
    return f"{APPLICATION_AUTHORITY_PREFIX}{digest}"


def is_application_authority_subject(subject: str) -> bool:
    return str(subject or "").strip().startswith(APPLICATION_AUTHORITY_PREFIX)


@dataclass(frozen=True)
class DescriptorControlCoordinates:
    """Where the one shared descriptor Control of an exact agent lives."""

    resource: str
    holder_subject: str
    control_id: str


def descriptor_control_coordinates(issuer_ref: str) -> DescriptorControlCoordinates:
    """Compute the holder and the Control id from the agent resource alone."""

    parsed = ApplicationResource.parse(issuer_ref)
    if not parsed.exact:
        raise ApplicationResourceError("application_resource_not_exact")
    holder = application_authority_subject(
        tenant=parsed.tenant, project=parsed.project, application=parsed.application)
    return DescriptorControlCoordinates(
        resource=parsed.resource,
        holder_subject=holder,
        control_id=control_card_id_for_issuer(AGENT_DESCRIPTOR_ISSUER_KIND, parsed.resource, grantor_subject=holder),
    )


def descriptor_control_parent(card: CardAuthority) -> DescriptorControlCoordinates | None:
    """The exact Agent Card -> shared descriptor Control edge, or None.

    Admitted only when the binding is a descriptor binding, its holder is the
    one computed from its own ``issuer_ref``, and its Control id is the
    deterministic id for that agent and holder.
    """

    binding = card.control_card
    if binding is None or binding.issuer_kind != AGENT_DESCRIPTOR_ISSUER_KIND:
        return None
    try:
        expected = descriptor_control_coordinates(binding.issuer_ref)
    except (ApplicationResourceError, ControlCardError):
        return None
    if binding.holder_subject != expected.holder_subject or binding.control_id != expected.control_id:
        return None
    return expected


def descriptor_edge_matches(card: CardAuthority, control: CardAuthority) -> bool:
    """Whether ``control`` is exactly the shared Control ``card`` must reach."""

    expected = descriptor_control_parent(card)
    return (
        expected is not None
        and control.grantor_subject == expected.holder_subject
        and control.access_id == expected.control_id
        and control.issuer_kind == AGENT_DESCRIPTOR_ISSUER_KIND
        and control.issuer_ref == expected.resource
        and control.control_card is None
    )


def descriptor_families_admitted(card: CardAuthority, control: CardAuthority) -> bool:
    """Whether the descriptor's resource families may admit ``card``'s own connectors.

    A family admits resources the Control does not list, so it widens. It is
    honoured on the Card owner's own Control (the per-user copies that exist
    until migration) and on the exact shared descriptor edge, never on any
    other cross-owner edge.
    """

    if control.grantor_subject == card.grantor_subject:
        return True
    return descriptor_edge_matches(card, control)


__all__ = [
    "APPLICATION_AUTHORITY_PREFIX",
    "DescriptorControlCoordinates",
    "application_authority_subject",
    "descriptor_control_coordinates",
    "descriptor_control_parent",
    "descriptor_edge_matches",
    "descriptor_families_admitted",
    "is_application_authority_subject",
]

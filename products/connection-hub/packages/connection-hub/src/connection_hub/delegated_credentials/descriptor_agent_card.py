# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Recognize a resident agent's stored descriptor Control before a Hub write.

The descriptor supplied to sync is not a second authorization source. The
current stored Control owns the ceiling, and a write must reserve its revision
until the transaction is decided.
"""

from __future__ import annotations

import re
from typing import Any

from .agent_capability_control import AGENT_DESCRIPTOR_ACCEPTANCE_KIND, AGENT_DESCRIPTOR_ISSUER_KIND
from .agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY,
    AgentCapabilityPolicy, AgentCapabilityPolicyError, descriptor_control,
)
from .application_resources import ApplicationResourceError, validate_application_card_resource
from .cards.identity import CARD_KIND_AGENT, CARD_KIND_CONTROL, resident_client_id, stable_resident_access_id
from .cards.model import CARD_STATE_ACTIVE, CONTROL_COMPOSITION_AND, CardAuthority, authority_is_credentialless
from .controls.model import control_card_id_for_issuer
from .controls.snapshot import control_snapshot_is_exact
from .controls.effective import ControlCardMismatch, effective_card_authority
from .oauth.grants import integration_subject


class DescriptorAgentCardRefused(ValueError):
    """A bounded, non-secret reason a descriptor Card cannot be written."""

    def __init__(self, reason: str, *, retryable: bool = False, status: int = 409) -> None:
        super().__init__(reason)
        self.reason, self.retryable, self.status = reason, retryable, status

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.reason, "retryable": self.retryable, "status": self.status}


def descriptor_agent_resource(
    resident: CardAuthority, *, tenant: str, project: str, grantor_subject: str,
) -> str:
    """Validate the resident and its exact deployment/owner/app/agent binding."""

    if resident.grantor_subject != grantor_subject:
        raise DescriptorAgentCardRefused("agent_capability_card_not_owned", status=403)
    binding = resident.control_card
    if binding is None or binding.issuer_kind != AGENT_DESCRIPTOR_ISSUER_KIND:
        raise DescriptorAgentCardRefused("agent_capability_card_required")
    try:
        resource = validate_application_card_resource(binding.issuer_ref, tenant=tenant, project=project, exact=True)
    except ApplicationResourceError as exc:
        raise DescriptorAgentCardRefused("agent_capability_control_deployment_mismatch") from exc
    if resource is None:
        raise DescriptorAgentCardRefused("agent_capability_control_identity_conflict")
    client = resident_client_id(resource.application, resource.agent)
    if (
        resident.source != "agent" or resident.card_kind != CARD_KIND_AGENT
        or resident.state != CARD_STATE_ACTIVE or resident.client_id != client
        or resident.access_id != stable_resident_access_id(grantor_subject, client)
        or resident.delegate_subject != integration_subject(grantor_subject, client_id=client)
        or binding.holder_subject not in ("", grantor_subject)
        or binding.control_id != control_card_id_for_issuer(
            AGENT_DESCRIPTOR_ISSUER_KIND, resource.resource, grantor_subject=grantor_subject)
    ):
        raise DescriptorAgentCardRefused("agent_capability_card_identity_conflict")
    try:
        selection = AgentCapabilityPolicy.from_property(
            resident.properties.get(AGENT_CAPABILITY_SELECTION_PROPERTY))
    except AgentCapabilityPolicyError as exc:
        raise DescriptorAgentCardRefused("agent_capability_selection_invalid") from exc
    if selection.resource != resource.resource:
        raise DescriptorAgentCardRefused("agent_capability_selection_resource_mismatch")
    return resource.resource


def validate_descriptor_agent_control(
    resident: CardAuthority, control: CardAuthority, *, tenant: str, project: str, grantor_subject: str,
) -> AgentCapabilityPolicy:
    """Return the current stored ceiling; a stale binding never adopts a newer Control."""

    resource = descriptor_agent_resource(resident, tenant=tenant, project=project, grantor_subject=grantor_subject)
    binding = resident.control_card
    assert binding is not None  # descriptor_agent_resource checked this
    if control.state != CARD_STATE_ACTIVE:
        raise DescriptorAgentCardRefused("agent_capability_control_not_active")
    if (
        control.access_id != binding.control_id or control.grantor_subject != grantor_subject
        or control.source != "control" or control.card_kind != CARD_KIND_CONTROL
        or not authority_is_credentialless(control) or control.control_card is not None
        or control.client_id != f"control-card:{AGENT_DESCRIPTOR_ISSUER_KIND}"
        or control.delegate_subject or control.issuer_kind != AGENT_DESCRIPTOR_ISSUER_KIND
        or control.issuer_ref != resource or control.composition_mode != CONTROL_COMPOSITION_AND
    ):
        raise DescriptorAgentCardRefused("agent_capability_control_identity_conflict")
    if binding.control_revision != control.card_revision:
        raise DescriptorAgentCardRefused("agent_capability_control_revision_conflict", retryable=True)
    try:
        marker = descriptor_control(control.properties)
        ceiling = AgentCapabilityPolicy.from_property(control.properties.get(AGENT_CAPABILITY_AUTHORITY_PROPERTY))
    except AgentCapabilityPolicyError as exc:
        raise DescriptorAgentCardRefused("agent_capability_control_evidence_invalid") from exc
    evidence = control.resource_acceptance.get(resource)
    if (
        marker is None or marker.resource != resource or ceiling.resource != resource
        or evidence is None or evidence.kind != AGENT_DESCRIPTOR_ACCEPTANCE_KIND
        or evidence.provider != AGENT_DESCRIPTOR_ISSUER_KIND or not evidence.revision
        or re.fullmatch(r"[0-9a-f]{64}", evidence.digest) is None
        or not control_snapshot_is_exact(control)
    ):
        raise DescriptorAgentCardRefused("agent_capability_control_evidence_invalid")
    return ceiling


def validate_descriptor_agent_additions(
    original: CardAuthority, candidate: CardAuthority, control: CardAuthority,
) -> None:
    """New standard authority must survive the stored ceiling's composition.

    Existing choices hidden by a withdrawn ceiling may remain as preferences;
    they are not a way to add authority that the Control does not grant.
    Resource-family bounds use the same composition as admission.
    """
    try:
        effective = effective_card_authority(candidate, control)
    except (ControlCardMismatch, AgentCapabilityPolicyError) as exc:
        raise DescriptorAgentCardRefused("agent_capability_control_mismatch") from exc
    for name in ("resource_grants", "resource_operations"):
        before, after, bounded = (getattr(card, name) for card in (original, candidate, effective))
        for resource, selected in after.items():
            if not set(selected).difference(before.get(resource, ())) <= set(bounded.get(resource, ())):
                raise DescriptorAgentCardRefused("agent_capability_selection_outside_control", status=403)
    before, after, bounded = (card.named_service_operations for card in (original, candidate, effective))
    if after.is_all or after.is_unknown:
        if after != before:
            raise DescriptorAgentCardRefused("agent_capability_selection_outside_control", status=403)
        return
    for resource, namespaces in after.operations.items():
        for namespace, operations in namespaces.items():
            prior = before.operations.get(resource, {}).get(namespace, ())
            allowed = bounded.operations.get(resource, {}).get(namespace, ())
            if not set(operations).difference(prior) <= set(allowed):
                raise DescriptorAgentCardRefused("agent_capability_selection_outside_control", status=403)

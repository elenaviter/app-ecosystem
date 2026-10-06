"""W502: the project host's answers carry its exact project Control Card (P) to the Hub.

Problem Board names P from its own stored link, in the membership answer behind
every project-person decision and in the invitation binding evidence. The Hub
takes exactly ``control_id`` and ``holder_subject`` or refuses the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connection_hub.delegated_credentials.project_authorization import ProjectAuthorizationError
from connection_hub.delegated_credentials.project_invitation_binding import ProjectInvitationBindingError
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
P = {"control_id": "p-control", "holder_subject": "project-creator"}


def _module(name):
    return load_dynamic_module_for_path(BUNDLE_ROOT / "services" / name)[1]


def _answer(key, body):
    async def call(**_kwargs):
        return {"ok": True, key: body}

    return call


def _membership(**extra):
    return {"project_ref": "work:project:q", "subject": "person-1", "role": "admin", **extra}


def _binding(**extra):
    return {"project_ref": "work:project:q", "invitation_ref": "inv-1", "control_id": "pending-1",
            "person_subject": "person-1", "email": "person@example.test", **extra}


@pytest.mark.asyncio
async def test_membership_carries_the_exact_project_control():
    m = _module("project_membership.py")
    port = m.BundleOperationProjectMembershipResolver(
        bundle_id="problem-board@1-0", operation="project_membership_resolve",
        caller=_answer("membership", _membership(project_control=P)))
    evidence = await port.resolve_project_membership(project_ref="work:project:q", subject="person-1")
    assert evidence.project_control.to_dict() == P
    none = m.BundleOperationProjectMembershipResolver(
        bundle_id="problem-board@1-0", operation="project_membership_resolve",
        caller=_answer("membership", _membership()))
    assert (await none.resolve_project_membership(project_ref="work:project:q", subject="person-1")).project_control is None


@pytest.mark.asyncio
async def test_a_malformed_project_control_refuses_the_membership_answer():
    m = _module("project_membership.py")
    port = m.BundleOperationProjectMembershipResolver(
        bundle_id="problem-board@1-0", operation="project_membership_resolve",
        caller=_answer("membership", _membership(project_control={"control_id": "p-control"})))
    with pytest.raises(ProjectAuthorizationError, match="project_control_locator_invalid"):
        await port.resolve_project_membership(project_ref="work:project:q", subject="person-1")


@pytest.mark.asyncio
async def test_invitation_evidence_carries_the_exact_project_control():
    m = _module("project_invitation_binding.py")
    port = m.BundleOperationProjectInvitationBindingResolver(
        bundle_id="problem-board@1-0", operation="project_invitation_binding_resolve",
        caller=_answer("binding", _binding(project_control=P)))
    evidence = await port.resolve_project_invitation_binding(project_ref="work:project:q", invitation_ref="inv-1")
    assert evidence.project_control.to_dict() == P
    bad = m.BundleOperationProjectInvitationBindingResolver(
        bundle_id="problem-board@1-0", operation="project_invitation_binding_resolve",
        caller=_answer("binding", _binding(project_control={**P, "extra": "x"})))
    with pytest.raises(ProjectInvitationBindingError, match="project_control_locator_invalid"):
        await bad.resolve_project_invitation_binding(project_ref="work:project:q", invitation_ref="inv-1")

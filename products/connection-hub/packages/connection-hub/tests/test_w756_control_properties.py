"""Whole-Card PLAN builders refuse changed Control properties before resolution."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.controls.snapshot import CONTROL_SNAPSHOT_PROPERTY
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.managed_card_selection_plan import (
    OPERATIONS,
    build_managed_card_selection_update,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused

from test_w607_existing_card_selection_plan import _Host, _cards, _propose
from test_w638_managed_card_selection_plan import PROJECT, project_control


@pytest.mark.parametrize("key", [CONTROL_SNAPSHOT_PROPERTY, "client_setting"])
def test_person_control_properties_cannot_hide_in_hub_recomputed_keys(key):
    _, control, _ = _cards()
    properties = copy.deepcopy(dict(control.properties or {}))
    properties[key] = {"untrusted": "browser replacement"}
    host = _Host()
    with pytest.raises(DecisionRefused, match="card_plan_undisplayed_change"):
        asyncio.run(_propose(host, control, selection={"properties": properties}))
    assert host.calls == []
    assert host.writes == 0


def _invitation_control():
    identity = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invite-1", target_email="someone@example.test")
    return bind_project_invitation_control(new_credentialless_card(
        grantor_subject=identity.project_subject, catalog_version="synthetic",
        control_id=identity.control_id, issuer_ref=identity.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, issuer_label="Invited",
        now=1_800_000_000), identity=identity)


@pytest.mark.parametrize("kind,card_factory", [
    ("project_control", project_control), ("invitation_control", _invitation_control),
])
def test_project_and_invitation_controls_refuse_changed_property_copies(kind, card_factory):
    original = card_factory()
    properties = copy.deepcopy(dict(original.properties or {}))
    properties["client_setting"] = "not editable on a Control"
    request = ProjectAuthorizationRequest.build(
        actor_subject="alice", project_ref=PROJECT, target_subject=original.access_id,
        operation=OPERATIONS[kind], request_id="one-save")
    decision = ProjectAuthorizationDecision.allow(request, platform_admin=True)
    host = _Host()
    with pytest.raises(DecisionRefused, match="card_plan_undisplayed_change"):
        asyncio.run(build_managed_card_selection_update(
            host, original=original, kind=kind, selection={"properties": properties},
            active=SimpleNamespace(version="synthetic"), decision=decision,
            project_ref=PROJECT, actor_subject="alice", request_id="one-save",
            now=1_800_000_001))
    assert host.calls == []
    assert host.writes == 0

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""W502 join: the invitee reads their pending invitation Card's revision and state, nothing else.

The join plans against the pending Card's exact revision before the invitee is
a member, so the admin-only invitation read cannot serve it. This read uses
``bind``'s own authority: the board's binding resolver answers for this
session's person and verified email, and the Card must name that email, this
project, invitation and control id, and still be pending. Nothing is written.
"""
from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.controls.project_invitation import ProjectInvitationControlIdentity

from test_project_invitation_access import (
    ADMIN, EMAIL, INVITATION_REF, PERSON, PROJECT_REF, _BindingResolver, _create, _Host, _lifecycle,
)


def _snapshot(host):
    return {key: (record.card_revision, record.content_hash(), state) for key, (record, state) in host.records.items()}


async def _read(lifecycle, *, actor=PERSON, project_ref=PROJECT_REF, invitation_ref=INVITATION_REF, control_id):
    return await lifecycle.pending_revision(actor_subject=actor, project_ref=project_ref,
                                            invitation_ref=invitation_ref, control_id=control_id)


@pytest.mark.asyncio
async def test_the_invitee_reads_only_the_pending_revision_and_state_and_nothing_is_written() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    created = await _create(lifecycle)
    before = _snapshot(host)
    answer = await _read(lifecycle, control_id=created["control_card"]["access_id"])
    assert answer == {"ok": True, "card_revision": 1, "state": "active"}
    assert _snapshot(host) == before  # nothing written


@pytest.mark.asyncio
async def test_an_admins_edit_moves_the_revision_the_invitee_reads() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    await lifecycle.update(actor_subject=ADMIN, project_ref=PROJECT_REF, invitation_ref=INVITATION_REF,
                           control_id=pending_id, request_id="request-update", label="Reviewed",
                           expected_card_revision=1)
    assert (await _read(lifecycle, control_id=pending_id))["card_revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("resolver", "actor", "error"), [
    (_BindingResolver(person=PERSON), "someone-else", "project_invitation_binding_invalid"),  # another session
    (_BindingResolver(email="other@example.test"), PERSON, "project_invitation_binding_email_mismatch"),
])
async def test_another_session_or_email_reads_nothing(resolver, actor, error) -> None:
    host = _Host()
    lifecycle = _lifecycle(host, resolver=resolver)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    answer = await _read(lifecycle, actor=actor, control_id=pending_id)
    assert answer["ok"] is False and answer["error"] == error and "card_revision" not in answer


class _NoEvidence(_BindingResolver):
    """The board returns no evidence: the session's email is unverified or not this invitation's."""

    async def resolve_project_invitation_binding(self, *, project_ref, invitation_ref):
        self.calls.append((project_ref, invitation_ref))
        return None


@pytest.mark.asyncio
async def test_without_the_boards_evidence_nothing_is_read() -> None:
    host = _Host()
    lifecycle = _lifecycle(host, resolver=_NoEvidence())
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    assert await _read(lifecycle, control_id=pending_id) == {
        "ok": False, "error": "project_invitation_binding_not_found", "status": 403}


@pytest.mark.asyncio
async def test_another_invitations_card_or_another_project_reads_nothing() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    other_card = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref="work:invitation:x",
                                                        target_email=EMAIL).control_id
    assert (await _read(lifecycle, control_id=other_card))["ok"] is False
    assert (await _read(lifecycle, project_ref="work:project:other", control_id=pending_id))["ok"] is False
    assert (await _read(lifecycle, control_id=""))["error"] == "project_invitation_control_id_mismatch"


@pytest.mark.asyncio
async def test_a_revoked_or_consumed_invitation_is_not_pending() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    await lifecycle.revoke(actor_subject=ADMIN, project_ref=PROJECT_REF, invitation_ref=INVITATION_REF,
                           control_id=pending_id, request_id="request-revoke")
    identity = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref=INVITATION_REF,
                                                      target_email=EMAIL)
    assert host.records[(identity.project_subject, pending_id)][1] == CARD_STATE_REVOKED
    assert await _read(lifecycle, control_id=pending_id) == {
        "ok": False, "error": "project_invitation_control_not_active", "status": 409}


@pytest.mark.asyncio
async def test_without_the_binding_resolver_the_read_is_retryably_unavailable() -> None:
    from connection_hub.delegated_credentials.project_invitation_access import ProjectInvitationControlLifecycle
    from test_project_invitation_access import _Port, _Record

    host = _Host()
    creator = _lifecycle(host)
    pending_id = (await _create(creator))["control_card"]["access_id"]
    lifecycle = ProjectInvitationControlLifecycle(host=host, authorization_port=_Port(), binding_resolver=None,
                                                  authority_from_record=lambda record: record.authority,
                                                  record_from_authority=_Record)
    answer = await _read(lifecycle, control_id=pending_id)
    assert answer["error"] == "project_invitation_binding_unavailable" and answer["retryable"] is True

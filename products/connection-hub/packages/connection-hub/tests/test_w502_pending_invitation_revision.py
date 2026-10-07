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


REFUSED = {"ok": False, "error": "project_invitation_pending_unavailable", "status": 403}


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
@pytest.mark.parametrize(("resolver", "actor"), [
    (_BindingResolver(person=PERSON), "someone-else"),  # another session
    (_BindingResolver(email="other@example.test"), PERSON),  # another email
])
async def test_another_session_or_email_reads_nothing(resolver, actor) -> None:
    host = _Host()
    lifecycle = _lifecycle(host, resolver=resolver)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    assert await _read(lifecycle, actor=actor, control_id=pending_id) == REFUSED


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
    assert await _read(lifecycle, control_id=pending_id) == REFUSED


@pytest.mark.asyncio
async def test_another_invitations_card_or_another_project_reads_nothing() -> None:
    host = _Host()
    lifecycle = _lifecycle(host)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    other_card = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref="work:invitation:x",
                                                        target_email=EMAIL).control_id
    assert await _read(lifecycle, control_id=other_card) == REFUSED
    assert await _read(lifecycle, project_ref="work:project:other", control_id=pending_id) == REFUSED
    assert await _read(lifecycle, control_id="") == REFUSED


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


@pytest.mark.asyncio
async def test_every_probe_gets_the_same_answer_so_no_invitation_existence_leaks() -> None:
    """Main/CodeApp: unknown ref, another project or Card, no evidence, another session or email: one answer."""
    host = _Host()
    lifecycle = _lifecycle(host)
    pending_id = (await _create(lifecycle))["control_card"]["access_id"]
    guessed = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref="work:invitation:guessed",
                                                     target_email=EMAIL).control_id
    probes = [
        # The board vouches for the session, but the Hub holds no such pending Card.
        await _read(lifecycle, invitation_ref="work:invitation:guessed", control_id=guessed),
        await _read(lifecycle, invitation_ref="work:invitation:guessed", control_id=pending_id),
        await _read(lifecycle, project_ref="work:project:other", control_id=pending_id),
        await _read(lifecycle, control_id="0" * 32),
        await _read(lifecycle, control_id=""),
        await _read(lifecycle, actor="someone-else", control_id=pending_id),
        await _read(_lifecycle(host, resolver=_NoEvidence()), control_id=pending_id),
        await _read(_lifecycle(host, resolver=_BindingResolver(email="other@example.test")), control_id=pending_id),
    ]
    assert probes == [REFUSED] * len(probes)

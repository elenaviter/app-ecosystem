"""W502: a person's project Control C is bound under the project's Control Card P.

``AutomationAccessService`` over ``DurableCardPersistence``, the real Card
store and Redis; only the project host's decision (which names P) and its
invitation evidence are stand-ins. P is a real application Control created by
``control_card_create`` under its creator, at the id derived from the project.

- create and redemption write C already bound under exactly the P the host
  names, in its first revision (no unbound C is ever visible), gated as a
  create of a bound Card; an existing C is bound through
  ``attach_control_card``; the chain composes C -> P with P's own operation;
- a locator whose id is not P's derived id, or whose P is absent, is refused
  before any C is written;
- the same P again is a no-op; a C bound to a live other P is ``p_conflict``,
  and is moved only after that P ended (EMain R3);
- editing P never rebinds C and leaves the chain valid (EMain R4);
- the repair operation reports each person's outcome (EMain R5);
- P is the project's top boundary (Root option (a)): a P under another Card is
  refused at bind, and a C -> P edge whose P gained a parent is invalid.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.project_invitation import ProjectInvitationControlIdentity
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectControlLocator,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingError,
    ProjectInvitationBindingEvidence,
)
from test_project_person_access import PROJECT_REF, TARGET
from test_w502_my_card_fence_real_path import GRANT, OPERATION, USER, _control, _select_control
from test_w580_bound_card_writers import _hub, _memories, redis_client  # noqa: F401 - fixture

CREATOR, OTHER_CREATOR = "project-creator", "next-project-creator"


def _locator(holder: str = CREATOR, *, control_id: str = "") -> ProjectControlLocator:
    return ProjectControlLocator(
        control_id=control_id or control_card_id_for_issuer("application", PROJECT_REF, grantor_subject=holder),
        holder_subject=holder)


class _Port:
    """The project host's decision (a stand-in), naming P as its own stored link does."""

    def __init__(self) -> None:
        self.locator: ProjectControlLocator | None = _locator()

    async def authorize_project_person_control(self, request):
        return ProjectAuthorizationDecision.allow(request, delegable_grants=(GRANT,), platform_admin=True,
                                                  evidence={"membership_revision": 7},
                                                  project_control=self.locator)


async def _service(tmp_path, redis_client):  # noqa: F811
    h = await _hub(tmp_path, redis_client)
    # The harness commits through a stand-in projection the resolver never reads; commit through
    # the real Redis projection the resolver serves, so a read after a write sees it, as live.
    persistence = h.service._persistence
    h.cards._cache = persistence._resolver._cache
    h.port = _Port()
    h.service._project_authorization_port = h.port
    h.service._bind_project_lifecycles()
    return h


async def _project_control(h, *, holder: str = CREATOR, mode: str = "and", grants: bool = True) -> str:
    """P: the project's application Control, created by its creator as the Problem Board writer does."""
    made = await h.service.control_card_create({"user_id": holder}, issuer_ref=PROJECT_REF,
                                               issuer_kind="application", composition_mode=mode)
    assert made["ok"] is True, made
    control_id = made["control_card"]["access_id"]
    assert control_id == _locator(holder).control_id
    if grants:
        updated = await h.service.control_card_update(
            {"user_id": holder}, control_id=control_id, resource_grants={_memories(): [GRANT]},
            resource_operations={_memories(): [OPERATION]}, _delegable_grants=(GRANT,))
        assert updated["ok"] is True, updated
    return control_id


async def _create(h, request_id="request-create"):
    return await h.service.project_person_control_create(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id=request_id, label="Quickstart member",
        migration=True)


async def _effective(h):
    """C's composed authority under its whole chain (raises on an invalid edge)."""
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    record = await h.service._load_record(identity.control_id, grantor_subject=identity.project_subject)
    return await h.service._compose_with_control(record)


async def _no_control_written(h):
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    return await h.store.read_current_authority(subject_hash=subject_hash_for(identity.project_subject),
                                                access_id=identity.control_id) is None


@pytest.mark.asyncio
async def test_create_binds_c_under_the_named_p_and_the_chain_composes_with_and(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    created = await _create(h)
    assert created["ok"] is True and created["project_control_binding"] == "bound", created
    binding = (await _control(h)).control_card
    assert (binding.control_id, binding.holder_subject, binding.issuer_kind, binding.issuer_ref) == (
        p_id, CREATOR, "application", PROJECT_REF)
    await _select_control(h)
    control, effective = await _effective(h)
    assert control.access_id == p_id and effective.resource_grants == {_memories(): (GRANT,)}


@pytest.mark.asyncio
async def test_p_and_that_selects_nothing_narrows_c_to_nothing(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h, grants=False)
    assert (await _create(h))["project_control_binding"] == "bound"
    await _select_control(h)
    control, effective = await _effective(h)
    assert control.access_id == p_id and not effective.resource_grants.get(_memories())


@pytest.mark.asyncio
async def test_p_or_composes_with_p_own_operation(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h, mode="or", grants=False)
    assert (await _create(h))["project_control_binding"] == "bound"
    await _select_control(h)
    control, effective = await _effective(h)
    # P OR with nothing selected leaves C's own selection: P's operation applies, not a forced AND.
    assert control.access_id == p_id and effective.resource_grants == {_memories(): (GRANT,)}


@pytest.mark.asyncio
async def test_a_locator_that_is_not_p_derived_id_is_refused_before_any_c(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    h.port.locator = _locator(control_id="control-not-derived")
    refused = await _create(h)
    assert refused == {"ok": False, "error": "project_control_locator_mismatch", "outcome": "p_invalid",
                       "status": 409}
    assert await _no_control_written(h)


@pytest.mark.asyncio
async def test_an_absent_p_is_refused_before_any_c(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    refused = await _create(h)
    assert refused["error"] == "project_control_absent" and refused["outcome"] == "p_invalid"
    assert await _no_control_written(h)


@pytest.mark.asyncio
async def test_a_holder_who_did_not_create_p_finds_no_p(tmp_path, redis_client):  # noqa: F811
    """The locator's holder must be P's creator: P under another holder is absent, never borrowed."""
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    h.port.locator = _locator(OTHER_CREATOR)
    assert (await _create(h))["error"] == "project_control_absent"
    assert await _no_control_written(h)


def test_an_empty_or_widened_locator_is_refused_at_the_boundary():
    for raw in ({"control_id": "p", "holder_subject": ""}, {"control_id": "", "holder_subject": CREATOR},
                {"control_id": "p", "holder_subject": CREATOR, "grantor_subject": CREATOR}, "p"):
        with pytest.raises(ProjectAuthorizationError, match="project_control_locator_invalid"):
            ProjectControlLocator.from_mapping(raw)
    assert ProjectControlLocator.from_mapping(None) is None
    with pytest.raises(ProjectInvitationBindingError, match="project_control_locator_invalid"):
        ProjectInvitationBindingEvidence.build(project_ref=PROJECT_REF, invitation_ref="i", control_id="c",
                                               person_subject=TARGET, email="a@example.test",
                                               project_control={"control_id": "p", "holder_subject": " "})


@pytest.mark.asyncio
async def test_binding_is_idempotent_and_the_repair_reports_already_bound(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    revision = (await _control(h)).card_revision
    again = await _create(h, "request-create-again")
    assert again["created"] is False and again["project_control_binding"] == "already_bound"
    repaired = await h.service.project_person_control_bind_project(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-repair")
    assert repaired == {"ok": True, "outcome": "already_bound", "person": TARGET}
    assert (await _control(h)).card_revision == revision


@pytest.mark.asyncio
async def test_the_repair_binds_an_existing_unbound_c(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    h.port.locator = None  # created before the project had a P
    created = await _create(h)
    assert created["ok"] is True and created["project_control_binding"] == "no_project_control"
    assert (await _control(h)).control_card is None
    refused = await h.service.project_person_control_bind_project(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-repair-none")
    assert refused["error"] == "project_control_locator_missing" and refused["person"] == TARGET
    p_id = await _project_control(h)
    h.port.locator = _locator()
    repaired = await h.service.project_person_control_bind_project(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-repair")
    assert repaired == {"ok": True, "outcome": "bound", "person": TARGET}
    assert (await _control(h)).control_card.control_id == p_id


@pytest.mark.asyncio
async def test_a_c_under_a_live_other_p_is_p_conflict_and_moves_only_after_that_p_ended(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    old_p = await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    new_p = await _project_control(h, holder=OTHER_CREATOR)
    h.port.locator = _locator(OTHER_CREATOR)
    conflict = await h.service.project_person_control_bind_project(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-rebind-live")
    assert conflict["outcome"] == "p_conflict" and conflict["error"] == "project_control_conflict"
    assert (await _control(h)).control_card.control_id == old_p
    revoked = await h.service.control_card_revoke({"user_id": CREATOR}, control_id=old_p)
    assert revoked["ok"] is True, revoked
    moved = await h.service.project_person_control_bind_project(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-rebind-ended")
    assert moved == {"ok": True, "outcome": "bound", "person": TARGET}
    binding = (await _control(h)).control_card
    assert (binding.control_id, binding.holder_subject) == (new_p, OTHER_CREATOR)


@pytest.mark.asyncio
async def test_editing_p_leaves_c_bound_and_valid(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    await _create(h)
    await _select_control(h)
    before = await _control(h)
    edited = await h.service.control_card_update(
        {"user_id": CREATOR}, control_id=p_id, label="Renamed project", resource_grants={_memories(): [GRANT]},
        resource_operations={_memories(): [OPERATION]}, _delegable_grants=(GRANT,))
    assert edited["ok"] is True, edited
    after = await _control(h)
    assert after.card_revision == before.card_revision and after.control_card == before.control_card
    control, effective = await _effective(h)
    assert control.access_id == p_id and effective.resource_grants == {_memories(): (GRANT,)}


@pytest.mark.asyncio
async def test_a_binding_to_another_projects_application_control_is_not_the_qualified_edge(tmp_path, redis_client):  # noqa: F811
    """Only the exact C -> P edge is qualified: another project's OR Control gets no union over C."""
    import dataclasses

    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    await _create(h)
    c = await _control(h)
    other = "work:project:someone-else"
    made = await h.service.control_card_create({"user_id": CREATOR}, issuer_ref=other, issuer_kind="application",
                                               composition_mode="or")
    foreign = dataclasses.replace(c.control_card, control_id=made["control_card"]["access_id"], issuer_ref=other)
    record = await h.service._load_record(c.access_id, grantor_subject=c.grantor_subject)
    with pytest.raises(ControlCardMismatch, match="control_card_foreign_holder_requires_and"):
        await h.service._compose_with_control(dataclasses.replace(record, control_card=foreign))


@pytest.mark.asyncio
async def test_redemption_binds_the_live_c_under_the_evidence_p(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    invitation_ref, email = "invitation-1", "invited@example.test"
    pending = await h.service.project_person_control_create(
        USER, project_ref=PROJECT_REF, invitation_ref=invitation_ref, target_email=email,
        request_id="request-invite", label="Invited")
    assert pending["ok"] is True, pending
    pending_id = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref=invitation_ref,
                                                        target_email=email).control_id

    class _Resolver:
        locator = {"control_id": "control-not-derived", "holder_subject": CREATOR}

        async def resolve_project_invitation_binding(self, *, project_ref, invitation_ref):
            return ProjectInvitationBindingEvidence.build(
                project_ref=project_ref, invitation_ref=invitation_ref, control_id=pending_id,
                person_subject=TARGET, email=email, project_control=self.locator)

    resolver = _Resolver()
    h.service.bind_project_invitation_binding_resolver(resolver)
    redeem = dict(project_ref=PROJECT_REF, invitation_ref=invitation_ref, control_id=pending_id,
                  request_id="request-redeem")
    refused = await h.service.project_person_control_bind_invitation({"user_id": TARGET}, **redeem)
    assert refused["error"] == "project_control_locator_mismatch"
    assert await _no_control_written(h)  # no unbound C, and the invitation is not consumed:
    pending_identity = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref=invitation_ref,
                                                              target_email=email)
    _record, pending_state = await h.service._load_record_any_state(
        pending_id, grantor_subject=pending_identity.project_subject)
    assert pending_state == "active"
    resolver.locator = _locator().to_dict()
    bound = await h.service.project_person_control_bind_invitation({"user_id": TARGET}, **redeem)
    assert bound["ok"] is True and bound["project_control_binding"] == "bound", bound
    assert bound["control_card"]["control_card"]["control_id"] == p_id
    assert (await _control(h)).control_card.holder_subject == CREATOR
    again = await h.service.project_person_control_bind_invitation({"user_id": TARGET}, **redeem)
    assert again["ok"] is True and again["project_control_binding"] == "already_bound", again


@pytest.mark.asyncio
async def test_p_is_the_top_boundary_a_p_with_a_parent_is_refused(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    parent = await h.service.control_card_create({"user_id": CREATOR}, issuer_ref="work:org:above",
                                                 issuer_kind="application")
    attached = await h.service.attach_control_card({"user_id": CREATOR}, access_id=p_id,
                                                   control_id=parent["control_card"]["access_id"])
    assert attached["ok"] is True, attached
    refused = await _create(h)
    assert refused["error"] == "project_control_not_root" and refused["outcome"] == "p_invalid"
    assert await _no_control_written(h)


@pytest.mark.asyncio
async def test_a_bound_c_whose_p_gains_a_parent_is_invalid_not_extended(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    parent = await h.service.control_card_create({"user_id": CREATOR}, issuer_ref="work:org:above",
                                                 issuer_kind="application")
    attached = await h.service.attach_control_card({"user_id": CREATOR}, access_id=p_id,
                                                   control_id=parent["control_card"]["access_id"])
    assert attached["ok"] is True, attached
    with pytest.raises(ControlCardMismatch, match="project_control_not_root"):
        await _effective(h)


class _Policy:
    """The binding's writer policy (a stand-in for Problem Board's), recording what it decided."""

    def __init__(self, allow: bool) -> None:
        self.allow, self.requests = allow, []

    def _decision(self, request):
        from datetime import datetime, timedelta, timezone

        from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteDecision

        return CallerWriteDecision(self.allow, "" if self.allow else "pb_refused", "policy:v1",
                                   datetime.now(timezone.utc) + timedelta(minutes=5), request)

    async def decide(self, request):
        self.requests.append(request)
        return self._decision(request)

    async def revalidate(self, request, initial):
        return self._decision(request)

    async def finalize(self, request, *, state, card_revision):
        return True


def _bind_policy(h, allow):
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriterRegistry

    policy, registry = _Policy(allow), CallerWriterRegistry()
    registry.register("application", policy)
    h.service.bind_caller_writers(registry)
    return policy


@pytest.mark.asyncio
async def test_create_writes_no_unbound_c_revision(tmp_path, redis_client):  # noqa: F811
    """CodeApp 22:44: C is never visible unbound; its first revision already carries P."""
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    first = await _control(h)
    assert first.card_revision == 1 and first.control_card.control_id == p_id
    assert first.control_card.holder_subject == CREATOR


@pytest.mark.asyncio
async def test_a_bound_create_is_decided_by_the_binding_policy_as_a_create(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    policy = _bind_policy(h, allow=True)
    assert (await _create(h))["project_control_binding"] == "bound"
    creates = [r for r in policy.requests if r.action == "create"]
    assert len(creates) == 1
    assert (creates[0].binding_kind, creates[0].binding_ref, creates[0].card_revision, creates[0].actor_subject) == (
        "application", PROJECT_REF, 0, USER["user_id"])


@pytest.mark.asyncio
async def test_a_refused_bound_create_writes_nothing(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    _bind_policy(h, allow=False)
    refused = await _create(h)
    assert refused["ok"] is False and refused["error"] == "pb_refused"
    assert await _no_control_written(h)


@pytest.mark.asyncio
async def test_redemption_writes_no_unbound_c_revision(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    invitation_ref, email = "invitation-2", "invited2@example.test"
    assert (await h.service.project_person_control_create(
        USER, project_ref=PROJECT_REF, invitation_ref=invitation_ref, target_email=email,
        request_id="request-invite", label="Invited"))["ok"] is True
    pending_id = ProjectInvitationControlIdentity.build(project_ref=PROJECT_REF, invitation_ref=invitation_ref,
                                                        target_email=email).control_id

    class _Resolver:
        async def resolve_project_invitation_binding(self, *, project_ref, invitation_ref):
            return ProjectInvitationBindingEvidence.build(
                project_ref=project_ref, invitation_ref=invitation_ref, control_id=pending_id,
                person_subject=TARGET, email=email, project_control=_locator().to_dict())

    h.service.bind_project_invitation_binding_resolver(_Resolver())
    bound = await h.service.project_person_control_bind_invitation(
        {"user_id": TARGET}, project_ref=PROJECT_REF, invitation_ref=invitation_ref, control_id=pending_id,
        request_id="request-redeem")
    assert bound["ok"] is True and bound["project_control_binding"] == "bound", bound
    first = await _control(h)
    assert first.card_revision == 1 and first.control_card.control_id == p_id

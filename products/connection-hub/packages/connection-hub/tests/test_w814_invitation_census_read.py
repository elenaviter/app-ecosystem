"""W814: ``card_invitation_census_read`` enumerates one managed project's invitation Controls, signed.

The caller names only the scope; the Hub lists the project's partition, keeps the invitation-issuer Cards
whose identity names this project, and answers each with its exact revision and state (pending, withdrawn,
redeemed). A staged invitation Card or an invalid identity refuses the whole read.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards.invitation_census_read import (
    ANSWER_SCHEMA, OPERATION, REQUEST_SCHEMA, CardInvitationCensusReadOperation, invitation_census_request_digest,
    invitation_entries_root,
)
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.store import CardStorageError, subject_hash_for
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlIdentity, bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import project_authority_subject
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity
from service_foundation.coordination.participant_answer import AnswerContract, sign_participant_answer
from test_card_transaction_store import _setup

NOW = 1_800_000_000
PEER, PROJECT, OTHER_PROJECT = "problem-board", "work:project:one", "work:project:two"
REQUEST_SECRET, RECEIPT_SECRET = "r" * 40, "s" * 40


class _Nonces:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


async def _invitation(service, *, project=PROJECT, invitation_ref, email, state="active", bound=False):
    identity = ProjectInvitationControlIdentity.build(project_ref=project, invitation_ref=invitation_ref,
                                                      target_email=email)
    card = bind_project_invitation_control(new_credentialless_card(
        control_id=identity.control_id, grantor_subject=identity.project_subject, catalog_version="v1",
        issuer_ref=identity.invitation_ref, issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=NOW),
        identity=identity)
    subject = subject_hash_for(identity.project_subject)
    await service.commit(card, subject_hash=subject, expected_revision=0, now=NOW)
    if state == "revoked":
        provenance = dict(card.provenance or {})
        if bound:
            provenance["project_invitation_binding"] = {"person": "user:invitee"}
        revoked = dataclasses.replace(card, state=CARD_STATE_REVOKED, card_revision=card.card_revision + 1,
                                      provenance=provenance)
        await service.commit(revoked, subject_hash=subject, expected_revision=card.card_revision, now=NOW)
        return revoked
    return card


async def _world(tmp_path):
    store, service, before, _ = await _setup(tmp_path)
    # A person Control in the SAME partition: the read must skip it (not an invitation Control).
    person = ProjectPersonCardIdentity.build(project_ref=PROJECT, person_subject="user:admin-1")
    control = dataclasses.replace(before, access_id=person.control_id, grantor_subject=person.project_subject)
    await service.commit(control, subject_hash=subject_hash_for(person.project_subject), expected_revision=0, now=NOW)
    pending = await _invitation(service, invitation_ref="inv-pending", email="a@example.test")
    withdrawn = await _invitation(service, invitation_ref="inv-withdrawn", email="b@example.test", state="revoked")
    redeemed = await _invitation(service, invitation_ref="inv-redeemed", email="c@example.test", state="revoked",
                                 bound=True)
    other = await _invitation(service, project=OTHER_PROJECT, invitation_ref="inv-other", email="d@example.test")
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    operation = CardInvitationCensusReadOperation(callers={PEER: caller}, card_store=store, nonces=_Nonces(),
                                                  clock=lambda: NOW)
    return operation, store, {"pending": pending, "withdrawn": withdrawn, "redeemed": redeemed, "other": other}


def _request(*, scope=PROJECT, secret=REQUEST_SECRET):
    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": scope}
    proof = {"service_id": PEER, "timestamp": str(NOW), "nonce": os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"],
                                 request_digest=invitation_census_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    answer = dict(response["invitation_census_answer"])
    proof = answer.pop("receipt_proof")
    assert answer["schema"] == ANSWER_SCHEMA and answer["audience"] == "problem-board@1-0"
    assert answer["request_digest"] == invitation_census_request_digest(request)
    assert answer["request_echo"] == request["request_echo"] and answer["scope"] == request["scope"]
    expected = sign_participant_answer(answer, schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET,
                                       signer_id=proof["service_id"], timestamp=proof["timestamp"],
                                       contract=AnswerContract.INVITATION_CENSUS)
    assert expected["signature"] == proof["signature"]
    return answer["result"]


@pytest.mark.asyncio
async def test_the_hub_enumerates_exactly_this_projects_invitation_controls_with_revision_and_state(tmp_path):
    operation, _, cards = await _world(tmp_path)
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is True
    result = _verified(response, request)
    assert result["kind"] == "invitation_census"
    assert result["subject_hash"] == subject_hash_for(project_authority_subject(PROJECT))
    by_ref = {entry["invitation_ref"]: entry for entry in result["invitations"]}
    assert set(by_ref) == {"inv-pending", "inv-withdrawn", "inv-redeemed"}, "only this project's invitations"
    assert by_ref["inv-pending"] == {"access_id": cards["pending"].access_id, "invitation_ref": "inv-pending",
                                     "revision": cards["pending"].card_revision, "state": "pending"}
    assert by_ref["inv-withdrawn"]["state"] == "withdrawn" and by_ref["inv-withdrawn"]["revision"] == 2
    assert by_ref["inv-redeemed"]["state"] == "redeemed"
    assert [entry["access_id"] for entry in result["invitations"]] == sorted(by_ref[r]["access_id"] for r in by_ref)
    assert result["count"] == 3 and result["root"] == invitation_entries_root(result["invitations"])


@pytest.mark.asyncio
async def test_a_staged_invitation_card_refuses_the_whole_read_signed(tmp_path, monkeypatch):
    operation, store, cards = await _world(tmp_path)
    real = store.read_current_authority

    async def staged(*, subject_hash, access_id):
        if access_id == cards["pending"].access_id:
            raise CardStorageError("card_transaction_undecided")
        return await real(subject_hash=subject_hash, access_id=access_id)

    monkeypatch.setattr(store, "read_current_authority", staged)
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is False
    assert _verified(response, request) == {"kind": "refused", "code": "card_invitation_census_in_transaction",
                                            "status": 409}


@pytest.mark.asyncio
async def test_an_invitation_card_whose_identity_names_another_project_refuses_the_whole_read(tmp_path, monkeypatch):
    """A Card in THIS partition carrying another project's invitation identity is never skipped or answered."""
    operation, store, cards = await _world(tmp_path)
    foreign = cards["other"]  # its identity marker names OTHER_PROJECT
    real_list, real_read = store.list_card_ids, store.read_current_authority
    subject = subject_hash_for(project_authority_subject(PROJECT))

    async def listed(*, subject_hash):
        names = await real_list(subject_hash=subject_hash)
        return names + [foreign.access_id] if subject_hash == subject else names

    async def read(*, subject_hash, access_id):
        if subject_hash == subject and access_id == foreign.access_id:
            return None, dataclasses.replace(foreign, grantor_subject=project_authority_subject(PROJECT))
        return await real_read(subject_hash=subject_hash, access_id=access_id)

    monkeypatch.setattr(store, "list_card_ids", listed)
    monkeypatch.setattr(store, "read_current_authority", read)
    request = _request()
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "card_invitation_census_identity_invalid", "status": 409}


@pytest.mark.asyncio
async def test_scope_outside_the_callers_entitlement_and_a_replayed_nonce_are_refused(tmp_path):
    operation, _, _ = await _world(tmp_path)
    request = _request(scope="other:project:one")
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "card_invitation_census_scope_forbidden", "status": 403}
    request = _request()
    assert (await operation.answer(request))["ok"] is True
    replay = await operation.answer(request)
    assert replay == {"ok": False, "status": 401, "error": {"code": "card_participant_unauthenticated"}}
    forged = _request(secret="x" * 40)
    assert (await operation.answer(forged))["error"]["code"] == "card_participant_unauthenticated"

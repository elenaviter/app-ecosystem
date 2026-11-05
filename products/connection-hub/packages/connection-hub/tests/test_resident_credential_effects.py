"""Resident effect adapter contracts; SDK and SQL ports are test doubles here."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import time
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import CredentialIssueTarget
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from connection_hub.delegated_credentials.cards.participant_effects import ParticipantEffectApplier, ParticipantEffectRefused
from connection_hub.delegated_credentials.cards.resident_issuance import RESIDENT_ISSUANCE_KIND
from connection_hub.delegated_credentials.oauth_issuance import credential_issue_effect, effect_digest
from test_credentialless_card_persistence import _agent
from test_w603_credential_issue_effect import TX, GROUP, _receipt

BEARER = "synthetic-resident-original"
DIGEST = hashlib.sha256(BEARER.encode()).hexdigest()


class _Issuance:
    tenant, project = "synthetic-tenant", "synthetic-project"

    def __init__(self, raw, *, created):
        self.raw, self.decision_id = raw, GROUP if created else TX
        self.slot = {"state": "bound", "outcome": "", "token_sha256": DIGEST}
        self.activations = []

    async def read_issuance_plan(self, transaction_id):
        assert transaction_id == self.decision_id
        return {"transaction_id": transaction_id, "plan": deepcopy(self.raw)}

    async def bind_issued_credential(self, **kwargs):
        return "bound"

    async def activate_issued_credential(self, **kwargs):
        self.activations.append(kwargs)
        outcome = "applied" if kwargs["card_live"] else "superseded"
        self.slot.update(state="activated" if outcome == "applied" else "released", outcome=outcome)
        return outcome

    async def release_issued_credential(self, **kwargs):
        if self.slot:
            self.slot.update(state="released", outcome="released")
        return "released" if self.slot else "absent"

    async def issuance_reservations(self, transaction_id):
        assert transaction_id == self.decision_id
        return {"access": dict(self.slot)} if self.slot else {}


class _Provider:
    def __init__(self):
        self.activated, self.retired = [], []

    async def activate(self, *, plan, result):
        self.activated.append((plan, result))
        return CardCredentialHandles(access_id=plan.access_id, access_token=BEARER, session_id="synthetic-session")

    async def retire(self, *, plan, result):
        self.retired.append((plan, result))


class _Handles:
    def __init__(self, *, fail=False):
        self.fail, self.saved = fail, []

    async def write(self, card, handles):
        if self.fail:
            self.fail = False
            raise OSError("synthetic custody cut")
        self.saved.append((card, handles))


def _world(*, created=False, state="committed", fail=False):
    saved = _receipt(created=created, state=state)
    candidate = replace(_agent(), access_id="card_1", card_revision=saved["after"]["card_revision"],
                        expires_at=int(time.time()) + 604800)
    saved["after"].update(content_hash=candidate.content_hash(), expires_at=candidate.expires_at)
    issue = credential_issue_effect(access_id=candidate.access_id, slot="access", expires_at=candidate.expires_at,
                                    card_revision=candidate.card_revision)
    saved["effects"] = [issue]
    raw = dict(issuance_kind=RESIDENT_ISSUANCE_KIND, intent={"card_kind": "agent"},
        decision_request_id="1" * 64, original_input_digest="2" * 64,
        tenant=_Issuance.tenant, project=_Issuance.project, access_id=candidate.access_id,
        grantor_subject=candidate.grantor_subject, client_id=candidate.client_id,
        credential_issuer="synthetic-issuer", credential_subject="synthetic-subject",
        base_revision=0 if created else candidate.card_revision - 1, candidate_revision=candidate.card_revision,
        expires_at=candidate.expires_at, card_content_hash=candidate.content_hash(),
        delivery_deadline=candidate.expires_at, reserved_until=candidate.expires_at - 100,
        slots=["access"], effect_digests={"access": effect_digest(issue)})
    store = _Issuance(raw, created=created)
    provider, handles = _Provider(), _Handles(fail=fail)
    row = SimpleNamespace(state=state, intent=SimpleNamespace(digest=saved["intent_digest"]))

    class Decisions:
        async def read(self, transaction_id):
            assert transaction_id == store.decision_id
            return row

    def applier(*, available=True, live=candidate):
        target = CredentialIssueTarget(store, object(), handles, decisions=Decisions(),
                                       resident_credentials=provider if available else None)

        async def current(binding, payload):
            return live

        async def receipt(transaction_id):
            assert transaction_id == TX
            return deepcopy(saved)

        target._live_card = current
        return ParticipantEffectApplier(read_receipt=receipt, targets={"credential_issue": target})

    return SimpleNamespace(receipt=saved, candidate=candidate, issue=issue, store=store,
                           provider=provider, handles=handles, row=row, applier=applier)


async def _dispatch(w, phase="apply", **kwargs):
    return await getattr(w.applier(**kwargs), phase)("credential_issue", "access", w.issue["payload"],
                                                     transaction_id=TX)


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [False, True])
async def test_create_and_rotation_install_original_before_effect_reports_applied(created):
    w = _world(created=created)
    assert await _dispatch(w) == effect_digest(w.issue)
    assert len(w.handles.saved) == 1
    plan, result = w.provider.activated[0]
    assert plan.transaction_id == w.store.decision_id
    assert result.state == "committed" and result.per_slot["access"].token_sha256 == DIGEST
    assert w.handles.saved[0] == (w.candidate, CardCredentialHandles(
        access_id=w.candidate.access_id, access_token=BEARER, session_id="synthetic-session"))


@pytest.mark.asyncio
async def test_fresh_effect_replays_same_original_after_activation_and_custody_cut():
    w = _world(fail=True)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_unavailable"):
        await _dispatch(w)
    assert w.store.slot["state"] == "activated" and w.handles.saved == []
    # A fresh target/applier, as on a later recovery process; no preparation operation exists on this port.
    assert await _dispatch(w) == effect_digest(w.issue)
    assert len(w.handles.saved) == 1 and len(w.provider.activated) == 2
    assert w.provider.activated[0] == w.provider.activated[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_slot", [False, True])
async def test_abort_retires_original_even_after_cut_before_hub_reservation(missing_slot):
    w = _world(state="aborted")
    if missing_slot:
        w.store.slot = {}
    assert await _dispatch(w, "release") == effect_digest(w.issue)
    assert len(w.provider.retired) == 1 and w.provider.activated == [] and w.handles.saved == []
    assert w.provider.retired[0][1].state == "aborted"


@pytest.mark.asyncio
async def test_superseded_original_is_retired_without_activation_or_custody():
    w = _world()
    assert await _dispatch(w, live=None) == "superseded"
    assert w.provider.retired[0][1].per_slot["access"].outcome == "superseded"
    assert w.provider.activated == [] and w.handles.saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tenant", "foreign"), ("project", "foreign"), ("access_id", "another-card"),
    ("base_revision", 99), ("candidate_revision", 99), ("card_content_hash", "f" * 64),
    ("expires_at", 1), ("slots", ["access", "refresh"]),
    ("effect_digests", {"access": "f" * 64}),
])
async def test_changed_stored_plan_refuses_before_activating_a_slot_or_provider(field, value):
    w = _world()
    w.store.raw[field] = value
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await _dispatch(w)
    assert w.store.activations == [] and w.provider.activated == [] and w.handles.saved == []


@pytest.mark.asyncio
async def test_actual_decision_and_configured_adapter_are_required_before_prepare_or_apply():
    w = _world(state="prepared")
    w.row.state = "preparing"
    assert await _dispatch(w, "prepare") == effect_digest(w.issue)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await _dispatch(w, "prepare", available=False)
    w.receipt["state"] = "committed"
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await _dispatch(w)
    assert w.store.activations == [] and w.provider.activated == []


@pytest.mark.asyncio
async def test_a_resident_capable_host_requires_the_stored_plan_reader():
    w = _world(state="prepared")
    w.store.read_issuance_plan = None
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await _dispatch(w, "prepare")
    assert w.store.activations == [] and w.provider.activated == []

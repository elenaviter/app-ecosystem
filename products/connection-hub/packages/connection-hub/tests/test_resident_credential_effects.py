"""Resident effect adapter contracts; SDK and SQL ports are test doubles here."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import time
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import CredentialIssueTarget, GrantUnbindTarget
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
        self.now = int(time.time())

    async def issuance_clock(self):
        return self.now

    async def read_issuance_plan(self, transaction_id):
        assert transaction_id == self.decision_id
        return {"transaction_id": transaction_id, "plan": deepcopy(self.raw)}

    async def bind_issued_credential(self, **kwargs):
        return "bound"

    async def activate_issued_credential(self, **kwargs):
        self.activations.append(kwargs)
        if self.slot.get("state") == "activated":
            return "applied"  # the productive SQL slot never rolls back an activated outcome
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
        self.previous = []

    async def activate(self, *, plan, result):
        self.activated.append((plan, result))
        return CardCredentialHandles(access_id=plan.access_id, access_token=BEARER, session_id="synthetic-session")

    async def retire(self, *, plan, result):
        self.retired.append((plan, result))

    async def retire_previous(self, **kwargs):
        self.previous.append(kwargs)


class _Handles:
    def __init__(self, *, fail=False):
        self.fail, self.saved = fail, []

    async def write(self, card, handles):
        if self.fail:
            self.fail = False
            raise OSError("synthetic custody cut")
        self.saved.append((card, handles))

    async def binding_identity(self, access_id):
        if not self.saved:
            return None
        card, handles = self.saved[-1]
        assert access_id == card.access_id
        return {"from_fingerprint": hashlib.sha256(handles.access_token.encode()).hexdigest(),
                "from_revision": card.card_revision, "from_expires_at": card.expires_at}


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
        target._resident._read_card = current
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
async def test_lease_expiry_after_activation_cut_finishes_without_another_bearer_or_custody_write():
    w = _world(fail=True)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_unavailable"):
        await _dispatch(w)
    assert w.store.slot["state"] == "activated" and w.handles.saved == []
    w.store.now = w.candidate.expires_at
    assert await _dispatch(w, live=None) == "superseded"
    assert len(w.provider.activated) == 1 and w.provider.retired == [] and w.handles.saved == []


@pytest.mark.asyncio
async def test_moved_target_before_lease_expiry_never_reinstalls_an_activated_original():
    w = _world(fail=True)
    with pytest.raises(ParticipantEffectRefused):
        await _dispatch(w)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await _dispatch(w, live=None)
    assert len(w.provider.activated) == 1 and w.handles.saved == []


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
    ("delivery_deadline", 600), ("base_revision", True),
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


@pytest.mark.asyncio
async def test_rotation_retires_pinned_old_session_and_grant_after_install_with_replay():
    w = _world()
    old = {"kind": "grant_unbind", "key": "previous-resident", "payload": {
        "access_id": w.candidate.access_id, "session_id": "synthetic-old-session", "token_sha256": "d" * 64}}
    w.receipt["effects"].append(old)
    revoked = []

    class Grants:
        async def revoke_access_grant_by_digest(self, digest):
            assert w.handles.saved and w.provider.previous  # new custody first, then old session, then old grant
            revoked.append(digest)
            return "revoked"

    def applier():
        actual = w.applier()
        actual._targets["grant_unbind"] = GrantUnbindTarget(Grants(),
            resident=actual._targets["credential_issue"]._resident)
        return actual

    first = applier()
    await first.apply("credential_issue", "access", w.issue["payload"], transaction_id=TX)
    await first.apply("grant_unbind", old["key"], old["payload"], transaction_id=TX)
    await applier().apply("grant_unbind", old["key"], old["payload"], transaction_id=TX)
    assert revoked == ["d" * 64, "d" * 64]
    assert len(w.provider.previous) == 2
    assert w.provider.previous[0] == w.provider.previous[1]
    assert w.provider.previous[0]["session_id"] == "synthetic-old-session"
    assert w.provider.previous[0]["result"].per_slot["access"].token_sha256 == DIGEST


@pytest.mark.asyncio
async def test_old_session_retirement_refuses_a_false_decision_before_either_authority_changes():
    w = _world()
    old = {"kind": "grant_unbind", "key": "previous-resident", "payload": {
        "access_id": w.candidate.access_id, "session_id": "synthetic-old-session", "token_sha256": "d" * 64}}
    w.receipt["effects"].append(old)
    w.store.slot.update(state="activated", outcome="applied")
    w.row.state = "aborted"

    class Grants:
        async def revoke_access_grant_by_digest(self, digest):
            pytest.fail("false resident decision must not revoke an old grant")

    actual = w.applier()
    actual._targets["grant_unbind"] = GrantUnbindTarget(Grants(),
        resident=actual._targets["credential_issue"]._resident)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await actual.apply("grant_unbind", old["key"], old["payload"], transaction_id=TX)
    assert w.provider.previous == []


@pytest.mark.asyncio
async def test_recorded_effect_order_stops_at_custody_cut_and_cutoff_checks_installation(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.cards import transaction_store as tx

    w = _world(fail=True)
    old = {"kind": "grant_unbind", "key": "previous-resident", "payload": {
        "access_id": w.candidate.access_id, "session_id": "synthetic-old-session", "token_sha256": "d" * 64}}
    w.receipt["effects"].append(old)
    revoked = []

    class Grants:
        async def revoke_access_grant_by_digest(self, digest):
            revoked.append(digest)
            return "revoked"

    actual = w.applier()
    actual._targets["grant_unbind"] = GrantUnbindTarget(Grants(),
        resident=actual._targets["credential_issue"]._resident)
    marker_store = SimpleNamespace(root=tmp_path)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_unavailable"):
        await tx.apply_effects(marker_store, w.receipt, actual.apply)
    assert w.store.slot["state"] == "activated" and w.provider.previous == [] and revoked == []
    # Even an out-of-order direct internal invocation cannot skip new custody.
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await actual.apply("grant_unbind", old["key"], old["payload"], transaction_id=TX)
    assert w.provider.previous == [] and revoked == []

    async def completed(*args):
        return None

    # This test exercises the real recorded-effects loop; Card publication
    # fencing and real session authentication have separate integration tests.
    monkeypatch.setattr(tx, "_retire_pointer", completed)
    monkeypatch.setattr(tx, "_clear_marker", completed)
    await tx.apply_effects(marker_store, w.receipt, actual.apply)
    assert len(w.handles.saved) == 1 and len(w.provider.previous) == 1 and revoked == ["d" * 64]
    await tx.apply_effects(marker_store, w.receipt, actual.apply)
    assert len(w.provider.previous) == 1 and revoked == ["d" * 64]


@pytest.mark.asyncio
async def test_superseded_new_issuance_never_retires_old_session_while_a_card_is_live():
    w = _world()
    old = {"kind": "grant_unbind", "key": "previous-resident", "payload": {
        "access_id": w.candidate.access_id, "session_id": "synthetic-old-session", "token_sha256": "d" * 64}}
    w.receipt["effects"].append(old)
    w.store.slot.update(state="released", outcome="superseded")

    class Grants:
        async def revoke_access_grant_by_digest(self, digest):
            pytest.fail("superseded on a live Card must not revoke the old grant")

    actual = w.applier()
    actual._targets["grant_unbind"] = GrantUnbindTarget(Grants(),
        resident=actual._targets["credential_issue"]._resident)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await actual.apply("grant_unbind", old["key"], old["payload"], transaction_id=TX)
    assert w.provider.previous == []

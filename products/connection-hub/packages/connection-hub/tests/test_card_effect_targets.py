"""W582 composition: the Hub's concrete effect targets behind the validated applier."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import (
    NO_ACTIVE_CREDENTIALS, CredentialLifetimeTarget, GrantUnbindTarget, InvocationPolicyTarget,
    compose_card_effects,
)
from connection_hub.delegated_credentials.cards.participant_effects import (
    ParticipantEffectApplier, ParticipantEffectRefused,
)
from connection_hub.invocation_policy.models import POLICY_CHANGE_PREPARED, POLICY_ONCE
from test_invocation_policy import _authority, _service
from test_participant_effects import TX, receipt

OWNER = "synthetic-owner"


def _applier(saved, targets):
    async def read(transaction_id):
        return deepcopy(saved) if transaction_id == saved["transaction_id"] else None
    return ParticipantEffectApplier(read_receipt=read, targets=targets)


class _Grants:
    """The SQL grant store's two W582 operations, recorded."""

    def __init__(self, *, expiry="applied", unbind="revoked"):
        self.expiry, self.unbind = expiry, unbind
        self.expiries, self.unbinds = [], []

    async def set_card_credentials_expiry(self, access_id, expires_at):
        self.expiries.append((access_id, expires_at))
        return self.expiry

    async def revoke_access_grant_by_digest(self, token_sha256):
        self.unbinds.append(token_sha256)
        return self.unbind


def _lifetime_receipt():
    return receipt()  # credential_lifetime, key "access", absolute 200 on card_1 at base revision 1


@pytest.mark.asyncio
async def test_lifetime_sets_the_absolute_deadline_and_replays_to_the_same_digest():
    grants = _Grants()
    applier = _applier(_lifetime_receipt(), {"credential_lifetime": CredentialLifetimeTarget(grants)})
    effect = _lifetime_receipt()["effects"][0]
    first = await applier.apply(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    again = await applier.apply(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert first == again and len(first) == 64
    assert grants.expiries == [("card_1", 200), ("card_1", 200)]  # absolute; the store does not rewrite it


@pytest.mark.asyncio
async def test_lifetime_with_nothing_live_is_the_named_no_op_and_anything_else_refuses():
    effect = _lifetime_receipt()["effects"][0]
    quiet = _applier(_lifetime_receipt(), {"credential_lifetime": CredentialLifetimeTarget(
        _Grants(expiry=NO_ACTIVE_CREDENTIALS))})
    assert await quiet.apply(effect["kind"], effect["key"], effect["payload"],
                             transaction_id=TX) == NO_ACTIVE_CREDENTIALS
    odd = _applier(_lifetime_receipt(), {"credential_lifetime": CredentialLifetimeTarget(_Grants(expiry="maybe"))})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await odd.apply(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    bare = _applier(_lifetime_receipt(), {"credential_lifetime": CredentialLifetimeTarget(object())})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await bare.apply(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)


def _unbind_receipt(state="committed"):
    saved = receipt()
    saved["state"] = state
    saved["effects"] = [{"kind": "grant_unbind", "key": "old-bearer",
                         "payload": {"access_id": "card_1", "session_id": "bsn_old", "token_sha256": "d" * 64}}]
    return saved


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["revoked", "absent", "unbound"])
async def test_unbind_revokes_the_old_binding_by_its_pinned_digest(outcome):
    grants = _Grants(unbind=outcome)
    applier = _applier(_unbind_receipt(), {"grant_unbind": GrantUnbindTarget(grants)})
    effect = _unbind_receipt()["effects"][0]
    assert len(await applier.apply("grant_unbind", "old-bearer", effect["payload"], transaction_id=TX)) == 64
    assert grants.unbinds == ["d" * 64]


@pytest.mark.asyncio
async def test_unbind_and_lifetime_prepare_and_release_touch_no_target():
    grants = _Grants()
    for state, phase in (("prepared", "prepare"), ("aborted", "release")):
        saved = _unbind_receipt(state)
        applier = _applier(saved, {"grant_unbind": GrantUnbindTarget(grants)})
        await getattr(applier, phase)("grant_unbind", "old-bearer", saved["effects"][0]["payload"], transaction_id=TX)
    assert grants.unbinds == []


def _policy_receipt(state):
    authority = _authority(operation="restart")
    saved = receipt()
    saved["state"] = state
    saved["access_id"] = saved["before"]["access_id"] = saved["after"]["access_id"] = authority.access_id
    saved["subject_hash"] = hashlib.sha256(OWNER.encode("utf-8")).hexdigest()
    saved["effects"] = [{"kind": "invocation_policy", "key": authority.key,
                         "payload": {"owner_subject": OWNER, "authority": authority.to_dict(),
                                     "mode": POLICY_ONCE, "expected_revision": 0}}]
    return saved, authority


@pytest.mark.asyncio
async def test_policy_is_prepared_at_stage_published_at_commit_and_replayed_unchanged(tmp_path):
    policies = _service(tmp_path)
    prepared, authority = _policy_receipt("prepared")
    effect = prepared["effects"][0]
    await _applier(prepared, {"invocation_policy": InvocationPolicyTarget(policies)}).prepare(
        "invocation_policy", effect["key"], effect["payload"], transaction_id=TX)
    change = await policies.get_policy_change(owner_subject=OWNER, authority=authority)
    assert change.state == POLICY_CHANGE_PREPARED and change.change_id == f"{TX}:{authority.key}"
    committed, _ = _policy_receipt("committed")
    applier = _applier(committed, {"invocation_policy": InvocationPolicyTarget(policies)})
    first = await applier.apply("invocation_policy", effect["key"], effect["payload"], transaction_id=TX)
    again = await applier.apply("invocation_policy", effect["key"], effect["payload"], transaction_id=TX)
    assert first == again
    policy = await policies.get(owner_subject=OWNER, authority=authority)
    assert policy.mode == POLICY_ONCE and policy.revision == 1


@pytest.mark.asyncio
async def test_an_aborted_change_releases_only_its_prepared_policy_marker(tmp_path):
    policies = _service(tmp_path)
    prepared, authority = _policy_receipt("prepared")
    effect = prepared["effects"][0]
    await _applier(prepared, {"invocation_policy": InvocationPolicyTarget(policies)}).prepare(
        "invocation_policy", effect["key"], effect["payload"], transaction_id=TX)
    aborted, _ = _policy_receipt("aborted")
    releaser = _applier(aborted, {"invocation_policy": InvocationPolicyTarget(policies)})
    await releaser.release("invocation_policy", effect["key"], effect["payload"], transaction_id=TX)
    await releaser.release("invocation_policy", effect["key"], effect["payload"], transaction_id=TX)  # idempotent
    assert await policies.get_policy_change(owner_subject=OWNER, authority=authority) is None
    assert await policies.get(owner_subject=OWNER, authority=authority) is None  # nothing was published


@pytest.mark.asyncio
async def test_a_policy_prepared_on_a_moved_revision_is_refused_by_name(tmp_path):
    policies = _service(tmp_path)
    prepared, authority = _policy_receipt("prepared")
    await policies.set_policy(owner_subject=OWNER, authority=authority, mode=POLICY_ONCE)  # revision moves to 1
    effect = prepared["effects"][0]
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_revision_moved"):
        await _applier(prepared, {"invocation_policy": InvocationPolicyTarget(policies)}).prepare(
            "invocation_policy", effect["key"], effect["payload"], transaction_id=TX)


@pytest.mark.asyncio
async def test_composition_binds_all_three_hooks_and_never_mints():
    class _Service:
        def bind_effect_applier(self, f): self.applier = f
        def bind_effect_preparer(self, f): self.preparer = f
        def bind_effect_releaser(self, f): self.releaser = f

    service = _Service()
    applier = compose_card_effects(card_service=service, card_store=object(), grant_store=_Grants(), policies=None)
    assert (service.applier, service.preparer, service.releaser) == (applier.apply, applier.prepare, applier.release)
    saved = receipt()
    saved["effects"] = [{"kind": "grant_binding", "key": "slot-1", "payload": {
        "access_id": "card_1", "operations": ["search"], "resource_grants": {}, "resource_operations": {},
        "named_services": {}, "expires_at": 200, "slot": "slot-1"}}]
    minting = _applier(saved, dict(applier._targets))
    with pytest.raises(ParticipantEffectRefused, match="card_effect_mint_unqualified"):
        await minting.apply("grant_binding", "slot-1", saved["effects"][0]["payload"], transaction_id=TX)


@pytest.mark.asyncio
async def test_a_coordinated_prolong_applies_its_lifetime_through_the_composed_target(tmp_path):
    # End to end on the real path: W581 Coordinator, Hub participant, file Card store, gate,
    # and the composed applier reading the Card store's own receipt.
    from connection_hub.delegated_credentials.automation_access import ACCESS_SOURCE_OAUTH, record_from_card
    from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding
    from test_caller_writer_gate import Policy, _registry
    from test_card_coordinated_writes import _host
    from test_card_service import SUBJECT_HASH
    from test_card_transaction_store import _visible

    host, store, decisions, before, _ = await _host(tmp_path)
    hub = host._card_coordinator[0].participants[PARTICIPANT]
    bound = replace(before, card_revision=before.card_revision + 1, source=ACCESS_SOURCE_OAUTH,
                    control_card=ControlCardBinding(control_id="control-person", issuer_ref="work:project:one",
                                                    issuer_kind="project", control_revision=2))
    await hub._service.commit(bound, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision,
                              now=bound.created_at)

    class _LiveGrants(_Grants):
        async def card_credentials_live(self, access_id):
            return True

    grants = _LiveGrants()
    host._store = grants
    host._caller_writers = _registry(Policy(ttl=10**8))
    host._issuer_actor_subject, host._issuer_actor_subject_bound = "", False

    async def notify_change(subject, **kwargs):
        return None

    host.notify_change = notify_change
    compose_card_effects(card_service=hub._service, card_store=store, grant_store=grants, policies=None)
    result = await host._prolong_access({"sub": bound.grantor_subject}, record=record_from_card(bound),
                                        ttl_seconds=3600)
    assert result["ok"] is True, result
    assert grants.expiries == [(bound.access_id, result["access"]["expires_at"])]
    visible = await _visible(store, bound)
    assert visible.card_revision == bound.card_revision + 1

"""W585 host gap: the consent's invocation policies are part of the ONE issuance decision.

``begin_oauth_issuance(invocation_policies=...)`` validates them as
``apply_oauth_invocation_policies`` does and freezes one existing
``invocation_policy`` effect per selected outer operation beside the original
credentials. COMMIT applies them with the Card; a moved policy aborts the
whole decision; nothing is written by a second path. ``None`` is today's
issuance, digest included.

Real: everything of test_w603_original_issuance's world, plus the invocation
policy service and its effect target. DSN- and Redis-gated.
"""
from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.oauth_issuance import IssuanceRefused, original_input_digest
from connection_hub.invocation_policy import SURFACE_OUTER, InvocationAuthority

from test_w603_original_issuance import (
    CLIENT, GRANTOR, RESOURCE, _begin, _card, _lead_outcomes, _reserve, _usable, _world,
)

OPERATIONS = {RESOURCE: ["search"]}


def _authority(plan, operation="search"):
    return InvocationAuthority(access_id=plan.access_id, resource=RESOURCE, surface=SURFACE_OUTER,
                               operation=operation)


async def _existing_card(w):
    """A first consent without policies: the Card exists, so the next consent is a re-consent."""
    first = await _begin(w, request="exchange-1")
    await _reserve(w, first)
    await w.service.complete_oauth_issuance(transaction_id=first.transaction_id)
    return first


@pytest.mark.asyncio
async def test_a_reconsent_applies_its_policies_in_the_same_decision_as_its_card(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        first = await _existing_card(w)
        plan = await _begin(w, request="exchange-2", resource_operations=OPERATIONS,
                            invocation_policies={RESOURCE: {"search": "once"}})
        key = _authority(plan).key
        assert key in plan.effect_digests  # frozen in the plan beside the credentials
        assert await w.policies.get(owner_subject=GRANTOR, authority=_authority(plan)) is None  # nothing yet
        tokens = await _reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert (result.state, result.card_revision) == ("committed", 2)
        assert await _lead_outcomes(w, plan) == dict(plan.effect_digests)
        policy = await w.policies.get(owner_subject=GRANTOR, authority=_authority(plan))
        assert (policy.mode, policy.revision) == ("once", 1)
        assert await _usable(w, tokens) == {"access": True, "refresh": True}
        assert plan.access_id == first.access_id


@pytest.mark.asyncio
async def test_a_changed_policy_is_bound_to_its_current_revision(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        await _existing_card(w)
        one = await _begin(w, request="exchange-2", resource_operations=OPERATIONS,
                           invocation_policies={RESOURCE: {"search": "once"}})
        await _reserve(w, one)
        await w.service.complete_oauth_issuance(transaction_id=one.transaction_id)
        two = await _begin(w, request="exchange-3", resource_operations=OPERATIONS,
                           invocation_policies={RESOURCE: {"search": "always"}})
        await _reserve(w, two)
        assert (await w.service.complete_oauth_issuance(transaction_id=two.transaction_id)).state == "committed"
        policy = await w.policies.get(owner_subject=GRANTOR, authority=_authority(two))
        assert (policy.mode, policy.revision) == ("always", 2)


@pytest.mark.asyncio
async def test_a_policy_moved_after_planning_aborts_the_whole_decision(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        await _existing_card(w)
        plan = await _begin(w, request="exchange-2", resource_operations=OPERATIONS,
                            invocation_policies={RESOURCE: {"search": "once"}})
        # Someone else sets the policy between PLAN and COMMIT.
        await w.policies.set_policy(owner_subject=GRANTOR, authority=_authority(plan), mode="always",
                                    expected_revision=0)
        tokens = await _reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "aborted"
        assert (await _card(w, plan.access_id)).card_revision == 1  # the Card did not move
        assert await _usable(w, tokens) == {"access": False, "refresh": False}
        policy = await w.policies.get(owner_subject=GRANTOR, authority=_authority(plan))
        assert (policy.mode, policy.revision) == ("always", 1)  # the other writer's, untouched


@pytest.mark.asyncio
async def test_without_policies_the_issuance_and_its_digest_are_unchanged(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        plan = await _begin(w)
        inputs = {"grantor_subject": GRANTOR, "client_id": CLIENT, "client_label": "Claude Code",
                  "scopes": ["memories:read"], "operations": None, "resource_grants": None,
                  "resource_operations": None, "resource": RESOURCE, "access_id": "", "card_kind": "",
                  "identity_scope": "", "account_scope": None, "named_service_operations": None,
                  "catalog_version": "", "client_metadata": None, "properties": None,
                  "replace_authority": True, "expected_card_revision": None}
        assert plan.original_input_digest == original_input_digest(inputs)  # the field is absent
        assert set(dict(plan.effect_digests)) == {"access", "refresh"}


@pytest.mark.asyncio
async def test_policies_are_part_of_the_original_so_a_changed_replay_refuses(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        await _existing_card(w)
        values = dict(request="exchange-2", resource_operations=OPERATIONS)
        plan = await _begin(w, **values, invocation_policies={RESOURCE: {"search": "once"}})
        assert await _begin(w, **values, invocation_policies={RESOURCE: {"search": "once"}}) == plan
        with pytest.raises(IssuanceRefused, match="issuance_replay_changed"):
            await _begin(w, **values, invocation_policies={RESOURCE: {"search": "always"}})
        with pytest.raises(IssuanceRefused, match="issuance_replay_changed"):
            await _begin(w, **values)  # dropping them is a different original too


@pytest.mark.asyncio
@pytest.mark.parametrize("policies", [
    {},                                            # a selected operation without a policy
    {RESOURCE: {"search": "sometimes"}},           # not always/once
    {RESOURCE: {"search": "once", "write": "once"}},  # a policy for an operation not selected
])
async def test_policies_the_old_route_refuses_are_refused_before_anything_begins(tmp_path, policies):
    async with _world(tmp_path, with_policies=True) as w:
        await _existing_card(w)
        with pytest.raises(IssuanceRefused, match="issuance_invocation_policies_invalid"):
            await _begin(w, request="exchange-2", resource_operations=OPERATIONS, invocation_policies=policies)


@pytest.mark.asyncio
async def test_policies_without_the_policy_service_refuse_retryably(tmp_path):
    async with _world(tmp_path) as w:  # no policy service bound
        with pytest.raises(IssuanceRefused, match="issuance_invocation_policies_unavailable") as refused:
            await _begin(w, resource_operations=OPERATIONS, invocation_policies={RESOURCE: {"search": "once"}})
        assert refused.value.retryable is True


@pytest.mark.asyncio
async def test_a_first_consent_creates_its_card_and_its_offered_policies_in_one_decision(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        choices = {RESOURCE: {"search": "once", "write": "always"}}
        plan = await _begin(w, scopes=["memories:read", "memories:write"],
                            resource_operations={RESOURCE: ["search", "write"]}, invocation_policies=choices)
        assert plan.base_revision == 0
        tokens = await _reserve(w, plan)
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert (result.state, result.card_revision) == ("committed", 1)
        assert await _lead_outcomes(w, plan) == dict(plan.effect_digests)
        # The first consent's offered choices, exactly.
        stored = {operation: (await w.policies.get(owner_subject=GRANTOR,
                                                  authority=_authority(plan, operation))).mode
                  for operation in ("search", "write")}
        assert stored == {"search": "once", "write": "always"}
        assert await _usable(w, tokens) == {"access": True, "refresh": True}
        # A replayed completion is the same decision and writes nothing again.
        assert await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id) == result
        assert (await w.policies.get(owner_subject=GRANTOR, authority=_authority(plan))).revision == 1


@pytest.mark.asyncio
async def test_an_aborted_first_consent_leaves_no_card_and_no_policy(tmp_path):
    async with _world(tmp_path, with_policies=True) as w:
        plan = await _begin(w, resource_operations=OPERATIONS, invocation_policies={RESOURCE: {"search": "once"}})
        tokens = await _reserve(w, plan, slots=("access",))  # the refresh reservation is missing: ABORT
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id)
        assert result.state == "aborted"
        assert await _card(w, plan.access_id) is None
        assert await w.policies.get(owner_subject=GRANTOR, authority=_authority(plan)) is None
        assert await w.authority.get_access_grant_record(tokens["access"]) is None


@pytest.mark.asyncio
async def test_more_effects_than_one_decision_carries_refuse_before_anything_begins(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.cards import participant_effects

    async with _world(tmp_path, with_policies=True) as w:
        monkeypatch.setattr(participant_effects, "MAX_EFFECTS", 3)  # two credentials + two policies = 4
        with pytest.raises(IssuanceRefused, match="issuance_too_many_policies"):
            await _begin(w, scopes=["memories:read", "memories:write"],
                         resource_operations={RESOURCE: ["search", "write"]},
                         invocation_policies={RESOURCE: {"search": "once", "write": "once"}})
        from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
        from connection_hub.delegated_credentials.oauth_issuance import decision_request_id

        request = decision_request_id(scope=f"{PARTICIPANT}:oauth-issuance", grantor_subject=GRANTOR,
                                      client_id=CLIENT, original_request_id="exchange-1")
        assert await w.authority.read_issuance_plan_request(request) is None  # nothing was planned or stored

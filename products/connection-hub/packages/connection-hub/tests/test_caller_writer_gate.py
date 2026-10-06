"""W578: a bound owner write is decided by its binding's policy, before and inside the commit."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from connection_hub.delegated_credentials import caller_writer_gate as gate
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AUTOMATION
from connection_hub.delegated_credentials.cards.model import CardAuthority, ControlCardBinding, NamedServiceSelection

NOW = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)
RESOURCE = "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*"


def _card(*, bound=True, revision=3):
    return CardAuthority(
        access_id="my-person", client_id="project-person:one:person", grantor_subject="person",
        delegate_subject="person", source="manual", card_kind=CARD_KIND_AUTOMATION, card_revision=revision,
        catalog_version="catalog:1", state="active", created_at=1_700_000_000, expires_at=0,
        resource_grants={RESOURCE: ("work:review",)}, resource_operations={RESOURCE: ("plan.item.update",)},
        named_service_operations=NamedServiceSelection.none(),
        control_card=ControlCardBinding(control_id="control-person", issuer_ref="work:project:one",
                                        issuer_kind="project", control_revision=2) if bound else None,
    )


def _candidate(card, **changes):
    after = dataclasses.replace(card, card_revision=card.card_revision + 1, **changes)
    return after.to_dict()


class Policy:
    def __init__(self, *, allow=True, revalidate_allow=True, ttl=30, mismatch=False, raise_on=None):
        self.allow, self.revalidate_allow, self.ttl, self.mismatch, self.raise_on = (
            allow, revalidate_allow, ttl, mismatch, raise_on)
        self.calls = []

    def _decision(self, request, allowed):
        bound = dataclasses.replace(request, access_id="another") if self.mismatch else request
        return gate.CallerWriteDecision(allowed, "" if allowed else "pb_refused", "policy:v1",
                                        NOW + timedelta(seconds=self.ttl), bound)

    async def decide(self, request):
        self.calls.append(("decide", request))
        if self.raise_on == "decide":
            raise RuntimeError("policy down")
        return self._decision(request, self.allow)

    async def revalidate(self, request, initial):
        self.calls.append(("revalidate", request))
        if self.raise_on == "revalidate":
            raise RuntimeError("policy down")
        return self._decision(request, self.revalidate_allow)

    async def finalize(self, request, *, state, card_revision):
        self.calls.append(("finalize", state, card_revision))
        return True


def _registry(policy):
    registry = gate.CallerWriterRegistry()
    registry.register("project", policy)
    return registry


def _gate(registry, card, *, candidate=None, actor="person", action="update", request_id="r-1"):
    return asyncio.run(gate.caller_writer_before_commit(
        registry, card, actor_subject=actor, action=action,
        candidate=candidate if candidate is not None else _candidate(card), request_id=request_id,
        now=lambda: NOW))


def test_an_unbound_card_keeps_its_existing_path():
    policy = Policy()
    assert _gate(_registry(policy), _card(bound=False)) == (None, None)
    assert policy.calls == []


def test_a_card_bound_to_an_unregistered_kind_keeps_its_existing_path():
    assert _gate(gate.CallerWriterRegistry(), _card()) == (None, None)


def test_a_bound_write_is_decided_and_revalidated_inside_the_commit():
    policy = Policy()
    card = _card()
    before_commit, request = _gate(_registry(policy), card)
    assert [c[0] for c in policy.calls] == ["decide"]
    assert (request.actor_subject, request.action, request.access_id, request.card_revision) == (
        "person", "update", "my-person", 3)
    assert (request.binding_kind, request.binding_ref) == ("project", "work:project:one")
    assert request.change_digest == gate.change_digest(_candidate(card))
    asyncio.run(before_commit())
    assert [c[0] for c in policy.calls] == ["decide", "revalidate"]


def test_a_refused_decision_refuses_before_any_effect_and_finalizes_refused():
    policy = Policy(allow=False)
    with pytest.raises(gate.CallerWriteRefused) as raised:
        _gate(_registry(policy), _card())
    assert raised.value.reason == "pb_refused" and raised.value.outcome_confirmed is True
    assert policy.calls[-1] == ("finalize", "refused", 3)


def test_a_decision_withdrawn_before_the_commit_refuses_inside_it():
    policy = Policy(revalidate_allow=False)
    before_commit, _ = _gate(_registry(policy), _card())
    with pytest.raises(gate.CallerWriteRefused, match="pb_refused"):
        asyncio.run(before_commit())


@pytest.mark.parametrize("where", ["decide", "revalidate"])
def test_an_unavailable_policy_authorizes_nothing(where):
    policy = Policy(raise_on=where)
    if where == "decide":
        with pytest.raises(gate.CallerWriteRefused, match="caller_writer_policy_unavailable"):
            _gate(_registry(policy), _card())
    else:
        before_commit, _ = _gate(_registry(policy), _card())
        with pytest.raises(gate.CallerWriteRefused, match="caller_writer_policy_unavailable"):
            asyncio.run(before_commit())


def test_a_decision_for_another_request_or_an_expired_one_refuses():
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_decision_mismatch"):
        _gate(_registry(Policy(mismatch=True)), _card())
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_decision_expired"):
        _gate(_registry(Policy(ttl=0)), _card())


@pytest.mark.parametrize("field,value", [
    ("access_id", "other"), ("grantor_subject", "someone-else"), ("client_id", "other-client"),
    ("issuer_kind", "project"), ("expires_at", 1), ("delegate_subject", "other"),
])
def test_a_candidate_that_changes_the_targets_identity_is_refused_before_the_policy(field, value):
    policy = Policy()
    card = _card()
    candidate = _candidate(card)
    candidate[field] = value
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_candidate_binding_mismatch"):
        _gate(_registry(policy), card, candidate=candidate)
    assert policy.calls == []


def test_a_candidate_must_be_exactly_the_next_revision():
    card = _card()
    candidate = _candidate(card)
    candidate["card_revision"] = card.card_revision + 2
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_candidate_binding_mismatch"):
        _gate(_registry(Policy()), card, candidate=candidate)


@pytest.mark.parametrize("actor", ["", "anonymous", "integration:bot", "telegram_42"])
def test_only_an_authenticated_actor_can_write_a_bound_card(actor):
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_requires_authenticated_actor"):
        _gate(_registry(Policy()), _card(), actor=actor)


def test_the_actor_is_the_one_supplied_never_the_storage_owner():
    _, request = _gate(_registry(Policy()), _card(), actor="admin-who-hosts")
    assert request.actor_subject == "admin-who-hosts"


@pytest.mark.parametrize("action", ["revoke", "attach", "detach"])
def test_non_update_actions_bind_their_exact_change(action):
    card = _card()
    change = {"action": action, "access_id": card.access_id, "card_revision": card.card_revision}
    _, request = _gate(_registry(Policy()), card, candidate=change, action=action)
    assert request.action == action and request.change_digest == gate.change_digest(change)


def test_an_unknown_action_or_a_missing_request_id_is_refused():
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_action_invalid"):
        _gate(_registry(Policy()), _card(), action="rewrite")
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_request_id_required"):
        _gate(_registry(Policy()), _card(), request_id="")


def test_registration_is_once_per_kind_and_needs_the_whole_policy():
    registry = gate.CallerWriterRegistry()
    registry.register("project", Policy())
    with pytest.raises(ValueError):
        registry.register("project", Policy())
    with pytest.raises(ValueError):
        registry.register("other", object())


def test_the_outcome_is_finalized_with_the_write_state():
    policy = Policy()
    registry = _registry(policy)
    _, request = _gate(registry, _card())
    assert asyncio.run(gate.caller_write_outcome(registry, request, state="committed", card_revision=4)) == {
        "caller_write_outcome_confirmed": True}
    assert policy.calls[-1] == ("finalize", "committed", 4)
    assert asyncio.run(gate.caller_write_outcome(registry, None, state="committed", card_revision=4)) == {}


OTHER = "*/api/integrations/bundles/*/*/other-app@1-0/public/mcp/other*"


def _two_service_card():
    card = _card()
    return dataclasses.replace(card, resource_operations={RESOURCE: ("plan.item.update",), OTHER: ("x.read",)},
                               resource_grants={RESOURCE: ("work:review",), OTHER: ("other:read",)})


@pytest.mark.parametrize("operations,grants", [
    (("plan.item.update", "plan.item.create"), ("work:review", "work:write")),  # widen
    ((), ()),                                                                      # narrow to nothing
])
def test_reset_replaces_exactly_one_services_selection_with_the_controls(operations, grants):
    card = _two_service_card()
    after = gate.reset_candidate(card, resource=RESOURCE, control_operations=operations, control_grants=grants)
    assert after["resource_operations"][RESOURCE] == sorted(operations)
    assert after["resource_grants"][RESOURCE] == sorted(grants)
    assert after["resource_operations"][OTHER] == ["x.read"] and after["resource_grants"][OTHER] == ["other:read"]
    before = card.to_dict()
    for key in gate.PROTECTED_FIELDS:
        assert after.get(key) == before.get(key)
    assert after["card_revision"] == card.card_revision + 1


def test_a_reset_candidate_passes_the_gate_as_a_bound_reset():
    card = _two_service_card()
    after = gate.reset_candidate(card, resource=RESOURCE, control_operations=("plan.item.update",),
                                 control_grants=("work:review",))
    _, request = _gate(_registry(Policy()), card, candidate=after, action="reset")
    assert request.action == "reset" and request.change_digest == gate.change_digest(after)


def test_reset_needs_a_service_the_card_holds_and_a_resource():
    card = _card()
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_reset_service_not_held"):
        gate.reset_candidate(card, resource=OTHER, control_operations=(), control_grants=())
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_reset_resource_required"):
        gate.reset_candidate(card, resource=" ", control_operations=(), control_grants=())

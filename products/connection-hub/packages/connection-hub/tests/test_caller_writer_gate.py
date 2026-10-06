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
    if action == "revoke":
        change = {"action": action, "access_id": card.access_id, "card_revision": card.card_revision}
    else:  # attach and detach publish the whole next Card
        change = _candidate(card, control_card=None) if action == "detach" else _candidate(card)
    _, request = _gate(_registry(Policy()), card, candidate=change, action=action)
    assert request.action == action and request.change_digest == gate.change_digest(change)


# ── Ops B1 (12:14): the Control binding is pinned ──────────────────────────


@pytest.mark.parametrize("action", ["update", "reset", "replace", "extend", "oauth_grant", "renew", "fold",
                                    "control_snapshot", "prolong", "prune"])
@pytest.mark.parametrize("rebind", ["dropped", "other_issuer"])
def test_no_write_but_attach_or_detach_may_change_a_cards_binding(action, rebind):
    card = _card()
    other = None if rebind == "dropped" else ControlCardBinding(
        control_id="control-x", issuer_ref="work:project:another", issuer_kind="project", control_revision=1)
    candidate = _candidate(card, control_card=other)
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_binding_change_refused"):
        _gate(_registry(Policy()), card, candidate=candidate, action=action)


def test_attach_binds_an_unbound_card_or_keeps_its_issuer_but_never_moves_it():
    unbound, bound = _card(bound=False), _card()
    assert gate.binding_change_refusal("attach", unbound.to_dict(), bound.to_dict()) is None
    same_issuer = dataclasses.replace(bound, control_card=dataclasses.replace(bound.control_card, control_id="c-2"))
    assert gate.binding_change_refusal("attach", bound.to_dict(), same_issuer.to_dict()) is None
    moved = dataclasses.replace(bound, control_card=dataclasses.replace(bound.control_card, issuer_ref="work:project:x"))
    assert gate.binding_change_refusal("attach", bound.to_dict(), moved.to_dict()) == "caller_writer_binding_change_refused"
    assert gate.binding_change_refusal("detach", bound.to_dict(), bound.to_dict()) == "caller_writer_binding_change_refused"
    assert gate.binding_change_refusal("detach", bound.to_dict(), unbound.to_dict()) is None


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


# ── inside the real durable commit ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("revalidate_allow", [True, False])
async def test_the_gate_decides_inside_the_real_card_commit_and_a_refusal_leaves_no_effect(tmp_path, revalidate_allow):
    from contextlib import asynccontextmanager
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from test_card_service import _Cache, _authority, SUBJECT_HASH, NOW as CARD_NOW

    held = False

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    bound = dataclasses.replace(_authority(), control_card=ControlCardBinding(
        control_id="control-person", issuer_ref="work:project:one", issuer_kind="project", control_revision=2))
    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    await service.commit(bound, subject_hash=SUBJECT_HASH, expected_revision=0, now=CARD_NOW)
    candidate = dataclasses.replace(bound, card_revision=bound.card_revision + 1, label="edited by owner")
    policy = Policy(revalidate_allow=revalidate_allow)
    seen_inside = []
    original = policy.revalidate

    async def revalidate(request, initial):
        seen_inside.append(held)
        return await original(request, initial)

    policy.revalidate = revalidate
    before_commit, _ = await gate.caller_writer_before_commit(
        _registry(policy), bound, actor_subject="owner", action="update", candidate=candidate.to_dict(),
        request_id="r-1", now=lambda: NOW)
    if revalidate_allow:
        await service.commit(candidate, subject_hash=SUBJECT_HASH, expected_revision=1, now=CARD_NOW,
                             before_commit=before_commit)
        expected = candidate
    else:
        with pytest.raises(gate.CallerWriteRefused):
            await service.commit(candidate, subject_hash=SUBJECT_HASH, expected_revision=1, now=CARD_NOW,
                                 before_commit=before_commit)
        expected = bound
    assert seen_inside == [True]  # revalidated under the target lock
    current = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=bound.access_id)
    assert current[1] == expected and held is False


def test_a_required_binding_kind_with_no_policy_refuses_rather_than_falling_through():
    registry = gate.CallerWriterRegistry()
    registry.require("project")
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_policy_unavailable"):
        _gate(registry, _card())


def test_an_unrequired_unregistered_kind_keeps_the_existing_path():
    registry = gate.CallerWriterRegistry()
    registry.require("other-kind")
    assert _gate(registry, _card()) == (None, None)


def test_reset_also_resets_that_services_named_service_selection_only():
    named = NamedServiceSelection.exact({RESOURCE: {"work": ["object.get"]}, OTHER: {"x": ["object.list"]}})
    card = dataclasses.replace(_two_service_card(), named_service_operations=named)
    after = gate.reset_candidate(card, resource=RESOURCE, control_operations=("plan.item.update",),
                                 control_grants=("work:review",),
                                 control_named_services={"work": ["object.get", "object.search"]})
    selection = after["named_service_operations"]
    text = str(selection)
    assert "object.search" in text and "object.list" in text  # this service reset, the other kept


# ── W580/B: every writer of a bound Card names its action ──────────────────


class _Cards:
    def __init__(self, current):
        self.current = current

    async def load(self, access_id, *, subject_hash):
        return (self.current, None) if self.current is not None else None


class _Host:
    """The two attributes AutomationAccessService._enlisted_gate reads."""

    def __init__(self, registry, current):
        self._caller_writers = registry
        self._store = _Cards(current)

    def _cards(self):
        return self._store


async def _enlist(host, candidate, *, expected_revision, caller_write):
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    return await AutomationAccessService._enlisted_gate(host, candidate, expected_revision=expected_revision,
                                                        caller_write=caller_write)


@pytest.mark.asyncio
async def test_an_unnamed_write_of_a_bound_card_is_refused_before_any_effect():
    policy = Policy(ttl=10**8)
    card = _card()
    with pytest.raises(gate.CallerWriteRefused, match="caller_writer_not_enlisted"):
        await _enlist(_Host(_registry(policy), card), dataclasses.replace(card, card_revision=4),
                      expected_revision=3, caller_write=None)
    assert policy.calls == []


@pytest.mark.asyncio
async def test_an_unbound_card_needs_no_enlistment():
    card = _card(bound=False)
    assert await _enlist(_Host(_registry(Policy(ttl=10**8)), card), dataclasses.replace(card, card_revision=4),
                         expected_revision=3, caller_write=None) == (None, None)


@pytest.mark.asyncio
async def test_a_bound_create_is_decided_under_the_binding_it_takes():
    policy = Policy(ttl=10**8)
    card = _card(revision=1)
    before_commit, request = await _enlist(_Host(_registry(policy), None), card, expected_revision=0,
                                           caller_write=gate.CallerWrite("create", "person", "r-1"))
    assert request.action == "create" and request.card_revision == 0 and request.binding_kind == "project"
    await before_commit()
    assert [c[0] for c in policy.calls] == ["decide", "revalidate"]


@pytest.mark.asyncio
async def test_a_prolongation_may_only_move_expiry_forward():
    card = dataclasses.replace(_card(), expires_at=2_000_000_000)
    host = _Host(_registry(Policy(ttl=10**8)), card)
    ok = dataclasses.replace(card, card_revision=4, expires_at=2_000_003_600)
    _, request = await _enlist(host, ok, expected_revision=3, caller_write=gate.CallerWrite("prolong", "person"))
    assert request.action == "prolong"
    for bad in (dataclasses.replace(card, card_revision=4, expires_at=1_999_000_000),
                dataclasses.replace(card, card_revision=4, expires_at=2_000_003_600, label="also renamed")):
        with pytest.raises(gate.CallerWriteRefused, match="caller_writer_prolong_shape_invalid"):
            await _enlist(host, bad, expected_revision=3, caller_write=gate.CallerWrite("prolong", "person"))


def test_a_prune_may_only_remove_account_bindings():
    before = {"card_revision": 3, "account_scope": {"google": {"a1": ["mail"], "a2": ["mail"]}}}
    assert gate.candidate_shape_refusal("prune", before, {"card_revision": 4,
                                        "account_scope": {"google": {"a2": ["mail"]}}}) is None
    assert gate.candidate_shape_refusal("prune", before, {"card_revision": 4, "account_scope": {
        "google": {"a2": ["mail", "drive"]}}}) == "caller_writer_prune_shape_invalid"
    assert gate.candidate_shape_refusal("prune", before, {"card_revision": 4, "label": "x",
        "account_scope": {}}) == "caller_writer_prune_shape_invalid"


@pytest.mark.parametrize("action", ["extend", "oauth_grant", "renew", "fold", "control_snapshot"])
def test_no_system_writer_may_change_a_cards_identity(action):
    before = {"card_revision": 3, "access_id": "a", "client_id": "c", "grantor_subject": "g"}
    assert gate.candidate_shape_refusal(action, before, {**before, "card_revision": 4}) is None
    assert gate.candidate_shape_refusal(action, before, {**before, "card_revision": 4,
                                        "client_id": "other"}) == "caller_writer_candidate_binding_mismatch"

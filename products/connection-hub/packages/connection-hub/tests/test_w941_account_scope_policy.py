"""W941: a Control's account-scope policy, a generic Control property.

A Control with ``account_scope_policy: "caller"`` leaves the caller Card's
provider accounts as they are under AND (the descriptor-Control rule, made a
property); without it the intersection is unchanged, and OR is unchanged.
The rule is per Control: in a chain, a link without the property still
intersects. Only the Control's own transaction sets it; a direct write may
keep or remove it, never add it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, CardRecordError, NamedServiceSelection
from connection_hub.delegated_credentials.controls.account_scope_policy import (
    ACCOUNT_SCOPE_CALLER, ACCOUNT_SCOPE_POLICY_PROPERTY, DIRECT_WRITE_REFUSED,
    direct_write_refusal, keeps_caller_accounts,
)
from connection_hub.delegated_credentials.controls.effective import (
    compose_card_authority_selection, intersect_card_authority_selection,
)

RESOURCE = "https://board.example.test/mcp"
GRANT = "work:review"
OPERATION = "review.accept"
CALLER = {ACCOUNT_SCOPE_POLICY_PROPERTY: ACCOUNT_SCOPE_CALLER}


def _card(**changes) -> CardAuthority:
    values = dict(
        access_id="my-card", client_id="person", grantor_subject="person-1", delegate_subject="person-1",
        source="oauth", card_kind="automation",
        resource_grants={RESOURCE: (GRANT,)}, resource_operations={RESOURCE: (OPERATION,)},
        named_service_operations=NamedServiceSelection.exact({}),
        account_scope={"docs": {"account-1": [GRANT]}, "mail": {"account-2": [GRANT]}},
    )
    values.update(changes)
    return CardAuthority(**values)


def _ceiling(card: CardAuthority, *, access_id: str = "control", properties=None, account_scope=None):
    return dataclasses.replace(
        _control(card), access_id=access_id, grantor_subject="project",
        account_scope=account_scope if account_scope is not None else {}, properties=dict(properties or {}),
    )


def test_the_property_and_its_direct_write_rule() -> None:
    assert keeps_caller_accounts(CALLER) and not keeps_caller_accounts({}) and not keeps_caller_accounts(None)
    assert not keeps_caller_accounts({ACCOUNT_SCOPE_POLICY_PROPERTY: "anything"})
    assert direct_write_refusal({}, None) is None, "a write that keeps the stored map"
    assert direct_write_refusal(CALLER, dict(CALLER)) is None, "kept unchanged"
    assert direct_write_refusal(CALLER, {}) is None, "removed: that only narrows"
    refused = direct_write_refusal({}, dict(CALLER))
    assert refused["error"] == DIRECT_WRITE_REFUSED and refused["status"] == 409


def test_a_ceiling_with_the_caller_policy_keeps_the_callers_accounts() -> None:
    card = _card()
    kept = intersect_card_authority_selection(card, _ceiling(card, properties=CALLER))
    narrowed = intersect_card_authority_selection(card, _ceiling(card))

    assert kept.account_scope == {"docs": {"account-1": (GRANT,)}, "mail": {"account-2": (GRANT,)}}
    assert narrowed.account_scope == {}, "without the property a Control with no accounts removes them all"
    assert kept.resource_grants == narrowed.resource_grants, "only accounts change, never capability"


def test_the_policy_never_adds_an_account() -> None:
    card = _card(account_scope={"docs": {"account-1": [GRANT]}})
    ceiling = _ceiling(card, properties=CALLER, account_scope={"docs": {"account-9": [GRANT]}})
    assert intersect_card_authority_selection(card, ceiling).account_scope == {"docs": {"account-1": (GRANT,)}}


def test_in_a_chain_a_link_without_the_property_still_intersects() -> None:
    card = _card()
    with_policy = _ceiling(card, access_id="p", properties=CALLER)
    without = _ceiling(card, access_id="c", account_scope={"docs": {"account-1": [GRANT]}})

    through_both = intersect_card_authority_selection(intersect_card_authority_selection(card, with_policy), without)
    other_order = intersect_card_authority_selection(intersect_card_authority_selection(card, without), with_policy)

    assert through_both.account_scope == {"docs": {"account-1": (GRANT,)}}
    assert other_order.account_scope == {"docs": {"account-1": (GRANT,)}}


def _control(card: CardAuthority, **changes) -> CardAuthority:
    return dataclasses.replace(
        card, access_id="control", source="control", card_kind="control", client_id="control-card:synthetic",
        grantor_subject="person-1", delegate_subject="", issuer_ref="work:project:p", issuer_kind="application",
        account_scope={}, **changes,
    )


def test_composition_under_and_keeps_the_callers_accounts_and_or_is_unchanged() -> None:
    card = _card()
    kept = compose_card_authority_selection(card, _control(card, properties=CALLER))
    narrowed = compose_card_authority_selection(card, _control(card))
    union = compose_card_authority_selection(card, _control(card, properties=CALLER, composition_mode="or"))
    union_without = compose_card_authority_selection(card, _control(card, composition_mode="or"))

    assert kept.account_scope == {"docs": {"account-1": (GRANT,)}, "mail": {"account-2": (GRANT,)}}
    assert narrowed.account_scope == {}
    assert union.account_scope == union_without.account_scope, "OR keeps today's meaning"
    assert ACCOUNT_SCOPE_POLICY_PROPERTY not in dict(kept.properties or {}), "a Control's own policy, never inherited"


def test_the_codec_admits_only_caller_and_only_on_a_control_card() -> None:
    card = _card()
    _control(card, properties=CALLER)  # valid
    with pytest.raises(CardRecordError, match="control_card_account_scope_policy_invalid"):
        _control(card, properties={ACCOUNT_SCOPE_POLICY_PROPERTY: "owner"})
    with pytest.raises(CardRecordError, match="account_scope_policy_requires_control_card"):
        _card(properties=CALLER)


def test_the_planner_stores_the_policy_on_a_managed_control_only() -> None:
    from connection_hub.delegated_credentials.managed_card_selection_plan import (
        OPERATIONS, build_managed_card_selection_update,
    )
    from connection_hub.delegated_credentials.project_authorization import (
        ProjectAuthorizationDecision, ProjectAuthorizationRequest,
    )
    from service_foundation.coordination.durable_decision_log import DecisionRefused
    from test_w607_existing_card_selection_plan import _Host
    from test_w638_managed_card_selection_plan import PROJECT, project_control

    original = project_control()
    request = ProjectAuthorizationRequest.build(
        actor_subject="alice", project_ref=PROJECT, target_subject=original.access_id,
        operation=OPERATIONS["project_control"], request_id="policy-save")
    decision = ProjectAuthorizationDecision.allow(request, platform_admin=True)
    built = asyncio.run(build_managed_card_selection_update(
        _Host(), original=original, kind="project_control", selection={},
        active=SimpleNamespace(version=original.catalog_version), decision=decision,
        project_ref=PROJECT, actor_subject="alice", request_id="policy-save", now=1_800_000_001,
        account_scope_policy="caller"))
    member = built["member"]
    candidate = member.candidate if hasattr(member, "candidate") else member["candidate"]
    properties = dict((candidate.properties if hasattr(candidate, "properties") else candidate["properties"]) or {})
    assert properties[ACCOUNT_SCOPE_POLICY_PROPERTY] == "caller"

    class Refusing:
        async def _resolve_card_authority(self, **kwargs):
            pytest.fail("an invalid policy must refuse before any resolution")

    for kind, value in (("agent_card", "caller"), ("project_control", "owner")):
        with pytest.raises(DecisionRefused, match="card_plan_selection_invalid"):
            asyncio.run(build_managed_card_selection_update(Refusing(), original=project_control(), kind=kind,
                selection={}, active=None, decision=None, project_ref=PROJECT, actor_subject="alice",
                request_id="plan-1", now=1_800_000_000, account_scope_policy=value))


def test_the_lifecycle_plan_admits_the_policy_only_on_a_managed_controls_own_edit() -> None:
    import inspect

    from connection_hub.delegated_credentials import card_lifecycle_plan

    source = inspect.getsource(card_lifecycle_plan)
    assert '"reselect", "reselect_project_control", "reselect_invitation_control"}' in source
    assert 'account_scope_policy=raw.get("account_scope_policy")' in source


def test_a_direct_control_write_that_adds_the_policy_is_refused_and_removing_it_is_not() -> None:
    from connection_hub.delegated_credentials.automation_access import record_from_card
    from test_w638_managed_card_selection_plan import project_control as stored_p
    from test_w638_project_control_forward import service

    svc = service(managed=False)  # not a managed project: the direct Control write path
    record = record_from_card(stored_p())

    async def load_record(access_id, *, grantor_subject):
        return dataclasses.replace(record, grantor_subject=grantor_subject)

    svc._load_record = load_record
    user = {"user_id": record.grantor_subject}
    added = asyncio.run(svc.update_access(
        user, access_id=record.access_id, resource_grants=None,
        properties={**dict(record.properties or {}), **CALLER}))
    assert added["ok"] is False and added["error"] == DIRECT_WRITE_REFUSED

    created = asyncio.run(svc.control_card_create(
        user, issuer_ref="work:project:other", issuer_kind="application", properties=dict(CALLER)))
    assert created["ok"] is False and created["error"] == DIRECT_WRITE_REFUSED


def test_the_property_is_classified_and_its_codes_are_registered() -> None:
    from connection_hub.card_save_codes import CARD_SAVE_CODES
    from connection_hub.delegated_credentials.card_property_classes import AUTHORIZATION

    assert ACCOUNT_SCOPE_POLICY_PROPERTY in AUTHORIZATION
    for code in (DIRECT_WRITE_REFUSED, "control_card_account_scope_policy_invalid",
                 "account_scope_policy_requires_control_card"):
        assert code in CARD_SAVE_CODES

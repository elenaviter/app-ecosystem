"""Signed scopes bind the real planner's loaded targets, including no-op reads.

Real C/My/P authority and planner; catalog resolution and persistence are test
adapters. These checks are not a caller-to-project entitlement registry.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import CardLifecyclePlanOperation
from test_w578_lifecycle_plan_operation import _Port, _request as plan_request, _verified as verified_plan
from test_w661_card_version_operation import _operation, _request, _verified
from test_w661_role_selection_plan import _Host, _role, _world
from test_w607_existing_card_selection_plan import ACTOR, PROJECT, TARGET

OTHER_PROJECT = "work:project:another-project"


class _CountedHost(_Host):
    def __init__(self, *cards):
        super().__init__(*cards)
        self.card_reads = []
        self.catalog_reads = 0
        self.config_reads = 0

    def _cards(self):
        original = super()._cards()
        reads = self.card_reads

        class Cards:
            async def load_current(self, access_id, *, subject_hash):
                reads.append((subject_hash, access_id))
                return await original.load_current(access_id, subject_hash=subject_hash)

        return Cards()

    async def _active_catalog(self):
        self.catalog_reads += 1
        return await super()._active_catalog()

    async def _catalog_config(self, active, *, owner_subject):
        self.config_reads += 1
        return await super()._catalog_config(active, owner_subject=owner_subject)


class _NoStageStore:
    """Neither a refusal nor an already-applied answer may reach persistence."""

    async def stage(self, *args, **kwargs):
        pytest.fail("a scope refusal or a role no-op reached STAGE persistence")


def _fixture(card_kind):
    project, control, my = _world()
    if card_kind == "project":
        project = dataclasses.replace(project, resource_grants=control.resource_grants,
                                      resource_operations=control.resource_operations)
    card = {"control": control, "my": my, "project": project}[card_kind]
    host = _CountedHost(project, control, my)
    return host, card


def _assert_only_existing_reads(host, card, before):
    # No callback to a project host, no catalog config/resolver for a no-op,
    # exactly the catalog and the target that the original path already reads.
    assert host.card_reads == [(_role(card)["subject_hash"], card.access_id)]
    assert host.catalog_reads == 1
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0
    assert {key: value.to_dict() for key, value in host.by_id.items()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_card_version_own_project_noop_stays_already_applied_with_existing_io_only(card_kind):
    host, card = _fixture(card_kind)
    before = copy.deepcopy({key: value.to_dict() for key, value in host.by_id.items()})
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    request = _request(scope=PROJECT, actor_subject=ACTOR,
                       updates=[_role(card, add_grants=["work:read"])])
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "already_applied"}
    assert len(operation._nonces.keys) == 1
    _assert_only_existing_reads(host, card, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind,scope,target", [
    ("control", OTHER_PROJECT, TARGET),
    ("my", OTHER_PROJECT, TARGET),
    ("control", PROJECT, "someone-else"),
    ("my", PROJECT, "someone-else"),
    ("project", PROJECT, TARGET),
])
async def test_card_version_role_noop_cannot_disclose_a_foreign_target(card_kind, scope, target):
    host, card = _fixture(card_kind)
    before = copy.deepcopy({key: value.to_dict() for key, value in host.by_id.items()})
    operation, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    # Both project scopes pass the same configured prefix. Only the loaded
    # target's identity can tell whether this request names its own project.
    request = _request(scope=scope, actor_subject=ACTOR,
                       updates=[_role(card, target_subject=target, add_grants=["work:read"])])
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "edit_invalid", "status": 409}, result
    assert len(operation._nonces.keys) == 1  # authenticated refusal consumes just its documented nonce
    _assert_only_existing_reads(host, card, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("card_kind", ["control", "my"])
async def test_signed_lifecycle_plan_also_binds_role_noop_before_answering(card_kind):
    host, card = _fixture(card_kind)
    stage, _, _ = _operation(host=host, planner=plan_card_lifecycle, store=_NoStageStore())
    port = _Port()
    operation = CardLifecyclePlanOperation(
        callers=stage._callers, authorization=port, planner=plan_card_lifecycle,
        host=host, nonces=stage._nonces, clock=stage._clock)
    request = plan_request(creations=[], scope=OTHER_PROJECT, actor=ACTOR,
                           updates=[_role(card, add_grants=["work:read"])])
    result = verified_plan(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "card_plan_update_scope_invalid", "status": 409}, result
    assert len(port.requests) == 1  # its existing PLAN authorization; no second lookup
    assert len(operation._nonces.keys) == 1
    assert host.config_reads == 0 and host.calls == [] and host.writes == 0

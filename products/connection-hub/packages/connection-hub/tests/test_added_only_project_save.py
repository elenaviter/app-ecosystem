"""A project-bounded Card save refuses only what it adds beyond the bound (W296).

On 2026-09-26 a second project admin could not save any Control Card: the save
was checked against every permission the Card would carry, not only the ones
the editor added, so an editor bounded below what a Card already held could not
even accept a changed descriptor or remove a permission. The operator ruled
that an admin does what the owner does; the coordinator asked that a save keep
what the Card already carries and refuse, by name, only what it adds.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessRecord,
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.model import NamedServiceSelection
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections

RESOURCE = "problem_board"
CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {"grant": "work:review", "label": "Review", "delegable_roles": ["kdcube:role:registered"]},
                {"grant": "work:observe", "label": "Observe", "delegable_roles": ["kdcube:role:registered"]},
            ],
            "resources": [
                {
                    "resource": RESOURCE,
                    "label": "Problem Board",
                    "tools": {
                        "review.approve": {"label": "Approve", "grants": ["work:review"]},
                        "review.reject": {"label": "Reject", "grants": ["work:review"]},
                        "work.report": {"label": "Report", "grants": ["work:observe"]},
                    },
                }
            ],
        }
    }
}
USER = {"user_id": "second-admin", "roles": ["kdcube:role:registered"], "permissions": []}


def _service() -> AutomationAccessService:
    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=CONNECTIONS)

    return AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=oauth_delegated_config_from_connections(CONNECTIONS),
        catalog_resolver=_Resolver(),
    )


def _stored(operations: list[str], grants: list[str]) -> AutomationAccessRecord:
    return AutomationAccessRecord(
        access_id="aut_control",
        label="Control Card",
        client_id="client",
        grantor_subject="project",
        delegate_subject="person",
        card_kind="automation",
        operations=(),
        resource_grants={RESOURCE: tuple(grants)},
        resource_operations={RESOURCE: tuple(operations)},
        named_service_operations=NamedServiceSelection.none(),
    )


async def _save(existing, operations, grants, *, bound):
    service = _service()
    active = await service._catalog_resolver.resolve_active()
    return await service._resolve_card_authority(
        user=USER,
        existing=existing,
        active=active,
        resource_grants={RESOURCE: grants},
        resource_operations={RESOURCE: operations},
        operations=(),
        named_service_operations=None,
        account_scope=None,
        properties=None,
        _delegable_grants=bound,
    )


@pytest.mark.asyncio
async def test_a_grant_the_card_already_carries_stays_while_the_editor_adds_within_the_bound() -> None:
    stored = _stored(["review.approve"], ["work:review"])
    resolved = await _save(
        stored, ["review.approve", "work.report"], ["work:review", "work:observe"], bound=["work:observe"]
    )
    assert resolved.error is None


@pytest.mark.asyncio
async def test_removing_a_permission_needs_no_bound_over_what_stays() -> None:
    stored = _stored(["review.approve", "work.report"], ["work:review", "work:observe"])
    resolved = await _save(stored, ["review.approve"], ["work:review"], bound=[])
    assert resolved.error is None


@pytest.mark.asyncio
async def test_a_grant_the_save_adds_beyond_the_bound_is_refused_by_name() -> None:
    stored = _stored(["work.report"], ["work:observe"])
    resolved = await _save(
        stored, ["work.report", "review.approve"], ["work:observe", "work:review"], bound=["work:observe"]
    )
    assert resolved.error["error"] == "delegated_access_grants_not_delegable"
    assert resolved.error["grants"] == ["work:review"]
    assert "beyond what you may add to this Card: work:review" in resolved.error["message"]
    assert "delegable_roles" not in resolved.error["message"]


@pytest.mark.asyncio
async def test_a_new_operation_under_a_carried_grant_beyond_the_bound_is_refused() -> None:
    # Keeping a grant is not a licence to widen it: a newly selected operation
    # that carries a grant beyond the bound is an addition.
    stored = _stored(["review.approve"], ["work:review"])
    resolved = await _save(
        stored, ["review.approve", "review.reject"], ["work:review"], bound=["work:observe"]
    )
    assert resolved.error["grants"] == ["work:review"]


@pytest.mark.asyncio
async def test_narrowing_to_an_explicit_empty_selection_adds_nothing() -> None:
    # An explicit [] means no operations (the resolver stores it so and the
    # runtime enforces it), never every tool: removing the last operation of a
    # carried service is a narrowing, allowed under any bound.
    stored = _stored(["review.approve"], ["work:review"])
    resolved = await _save(stored, [], ["work:review"], bound=[])
    assert resolved.error is None
    assert resolved.resource_operations == {RESOURCE: []}


@pytest.mark.asyncio
async def test_selecting_from_a_stored_explicit_empty_selection_is_an_addition() -> None:
    # The Card held the grant but no operation: each operation selected now is new.
    stored = _stored([], ["work:review"])
    resolved = await _save(stored, ["review.approve"], ["work:review"], bound=["work:observe"])
    assert resolved.error["grants"] == ["work:review"]
    assert (await _save(stored, ["review.approve"], ["work:review"], bound=["work:review"])).error is None


@pytest.mark.asyncio
async def test_a_legacy_record_that_named_no_operations_keeps_an_exact_subset() -> None:
    stored = _stored([], ["work:review"])
    legacy = replace(stored, resource_operations={})
    resolved = await _save(legacy, ["review.approve"], ["work:review"], bound=[])
    assert resolved.error is None


@pytest.mark.asyncio
async def test_a_new_service_with_an_explicit_empty_selection_still_adds_its_grants() -> None:
    stored = _stored(["work.report"], ["work:observe"])
    service = _service()
    active = await service._catalog_resolver.resolve_active()
    resolved = await service._resolve_card_authority(
        user=USER, existing=replace(stored, resource_grants={}, resource_operations={}),
        active=active, resource_grants={RESOURCE: ["work:review"]}, resource_operations={RESOURCE: []},
        operations=(), named_service_operations=None, account_scope=None, properties=None,
        _delegable_grants=[],
    )
    assert resolved.error["grants"] == ["work:review"]


def test_the_classifier_tells_explicit_empty_from_a_missing_selection() -> None:
    from connection_hub.delegated_credentials.automation_access import _newly_selected_operation_grants

    config = oauth_delegated_config_from_connections(CONNECTIONS)
    pairs = [(RESOURCE, config.resource_config(RESOURCE))]
    stored = _stored(["review.approve"], ["work:review"])
    none = NamedServiceSelection.none()

    def added(selection):
        return _newly_selected_operation_grants(stored, resource_operations=selection,
                                                named_service_operations=none, resource_pairs=pairs)
    assert added({RESOURCE: []}) == set()
    assert added({}) is None  # nothing stated for operations the Card holds: not told apart
    assert added({RESOURCE: ["review.approve", "work.report"]}) == {"work:observe"}
    assert added({RESOURCE: ["*"]}) is None  # unknown growth stays unknown


def test_a_person_card_refusal_names_the_grants_and_the_project_host_s_reason() -> None:
    from connection_hub.delegated_credentials.project_person_access import _named_not_delegable

    reason = "A project admin may grant every operation Problem Board offers on a Card."
    decision = SimpleNamespace(
        evidence={"actor_membership": {"role": "admin", "evidence": {"delegation_bound": reason}}}
    )
    refused = _named_not_delegable(
        {"ok": False, "error": "delegated_access_grants_not_delegable", "grants": ["work:relay"], "message": "old"},
        decision,
    )
    assert refused["message"] == f"You cannot add work:relay to this Card. {reason}"
    assert refused["reason"] == reason
    assert "delegable_roles" not in refused["message"]
    # Without the host's reason it still names the grants, never the hub's capability list.
    bare = _named_not_delegable(
        {"ok": False, "error": "delegated_access_grants_not_delegable", "grants": ["work:relay"]},
        SimpleNamespace(evidence={}),
    )
    assert bare["message"] == "You cannot add work:relay to this Card. The project does not let you delegate them."
    other = {"ok": False, "error": "something_else"}
    assert _named_not_delegable(other, decision) == other

"""A project-bounded save keeps the application roles the Card already carries (W768).

On 2026-10-12 the project owner could not save any change on the project
Control Card. The Card's application-operations policy holds the platform
role ``kdcube:role:super-admin``; the project's one-Save path bounds a save by
the project's own grants, which never include a platform role. The role check
refused the whole Card (``application_roles_not_delegable``), although the
save changed no role. As for grants (W296), a save now refuses only the roles
it adds.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_OPERATIONS_PROPERTY,
)
from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessRecord,
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.model import NamedServiceSelection
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections

REGISTERED = "kdcube:role:registered"
PRIVILEGED = "kdcube:role:privileged"
SUPER = "kdcube:role:super-admin"
RESOURCE = "problem_board"
OP_READ = "urn:kdcube:application-operation:example%401-0:read"
OP_ADMIN = "urn:kdcube:application-operation:example%401-0:admin"
OP_EXPORT = "urn:kdcube:application-operation:example%401-0:export"
CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {"grant": "work:review", "label": "Review", "delegable_roles": [REGISTERED]},
                {"grant": "work:observe", "label": "Observe", "delegable_roles": [REGISTERED]},
            ],
            "resources": [
                {"resource": "*", "label": "All platform and application APIs",
                 "grants": [REGISTERED, PRIVILEGED, SUPER]},
                {
                    "resource": RESOURCE,
                    "label": "Problem Board",
                    "tools": {
                        "review.approve": {"label": "Approve", "grants": ["work:review"]},
                        "work.report": {"label": "Report", "grants": ["work:observe"]},
                    },
                },
            ],
        }
    }
}
USER = {"user_id": "project-owner", "roles": [REGISTERED], "permissions": []}
WIDENING_REFUSALS = {"application_roles_not_delegable", "delegated_access_grants_not_delegable"}
BOUND = ["work:review", "work:observe"]  # the project's own grants: never a platform role


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


def _policy(default_role: str, operation_roles: dict[str, str] | None = None) -> dict:
    return {
        APPLICATION_OPERATIONS_PROPERTY: {
            "schema": "kdcube.application_operations.v2",
            "mode": "selected",
            "default_role": default_role,
            "operation_roles": operation_roles or {},
        }
    }


def _stored(default_role: str = SUPER) -> AutomationAccessRecord:
    """The project Control as it stands: the board's operations plus a carried platform role."""
    return AutomationAccessRecord(
        access_id="control-project",
        label="Control Card",
        client_id="client",
        grantor_subject="project",
        delegate_subject="project",
        card_kind="automation",
        operations=(),
        resource_grants={"*": (default_role,), RESOURCE: ("work:review",)},
        resource_operations={"*": (OP_READ, OP_ADMIN), RESOURCE: ("review.approve",)},
        named_service_operations=NamedServiceSelection.none(),
        properties=_policy(default_role),
    )


async def _save(existing, *, operations, properties=None, bound=BOUND, application=(OP_READ, OP_ADMIN),
                default_role=None):
    service = _service()
    active = await service._catalog_resolver.resolve_active()
    grants = {"*": [default_role] if default_role else list(existing.resource_grants["*"]), RESOURCE: ["work:review", "work:observe"]
              if "work.report" in operations else ["work:review"]}
    return await service._resolve_card_authority(
        user=USER,
        existing=existing,
        active=active,
        resource_grants=grants,
        resource_operations={"*": list(application), RESOURCE: operations},
        operations=(),
        named_service_operations=None,
        account_scope=None,
        properties=properties,
        _delegable_grants=bound,
    )


@pytest.mark.asyncio
async def test_ticking_a_board_operation_keeps_the_carried_super_admin_role() -> None:
    # The live 23:16:25Z case: the owner ticks one more board operation.
    resolved = await _save(_stored(), operations=["review.approve", "work.report"])
    assert resolved.error is None


@pytest.mark.asyncio
async def test_a_save_that_changes_nothing_about_roles_needs_no_role_bound() -> None:
    resolved = await _save(_stored(), operations=["review.approve"], bound=[])
    assert resolved.error is None


@pytest.mark.asyncio
async def test_an_override_to_a_role_the_card_does_not_carry_is_still_refused() -> None:
    # Keeping a role is not a licence to add one: an operation override naming a
    # platform role the Card never held is an addition beyond the bound.
    stored = _stored(default_role=REGISTERED)
    resolved = await _save(
        stored,
        operations=["review.approve"],
        properties=_policy(REGISTERED, {OP_ADMIN: SUPER}),
    )
    assert resolved.error["error"] == "application_roles_not_delegable"


@pytest.mark.asyncio
async def test_rebinding_an_operation_to_a_carried_role_is_refused() -> None:
    # A role the Card carries as its default may not be reused for a new
    # per-operation binding: the policy changed, so only the bound decides.
    stored = _stored(default_role=SUPER)
    resolved = await _save(
        stored,
        operations=["review.approve"],
        properties=_policy(REGISTERED, {OP_ADMIN: SUPER}),
        default_role=REGISTERED,
    )
    # Refused by whichever bound sees the addition first (the grant or the role check).
    assert resolved.error["error"] in WIDENING_REFUSALS


@pytest.mark.asyncio
async def test_a_newly_selected_application_operation_under_the_carried_role_is_refused() -> None:
    # Keeping super-admin is not a licence to widen it to an operation the Card
    # did not hold (as for grants, W296).
    resolved = await _save(
        _stored(), operations=["review.approve"], application=(OP_READ, OP_ADMIN, OP_EXPORT)
    )
    # Refused by whichever bound sees the addition first (the grant or the role check).
    assert resolved.error["error"] in WIDENING_REFUSALS


@pytest.mark.asyncio
async def test_removing_an_application_operation_keeps_the_carried_role() -> None:
    resolved = await _save(_stored(), operations=["review.approve"], application=(OP_READ,))
    assert resolved.error is None


@pytest.mark.asyncio
async def test_an_unbounded_save_still_checks_every_role_against_the_editor() -> None:
    # Outside a project bound the editor's own delegable roles decide, as before.
    service = _service()
    active = await service._catalog_resolver.resolve_active()
    resolved = await service._resolve_card_authority(
        user=USER,
        existing=_stored(),
        active=active,
        resource_grants={"*": [SUPER], RESOURCE: ["work:review"]},
        resource_operations={"*": [OP_READ, OP_ADMIN], RESOURCE: ["review.approve"]},
        operations=(),
        named_service_operations=None,
        account_scope=None,
        properties=None,
    )
    assert resolved.error is not None

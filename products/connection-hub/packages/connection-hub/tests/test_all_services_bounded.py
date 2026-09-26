"""The "All platform and application APIs" row, bounded by the approver (operator, 2026-09-26).

The operator: "that must be for all, with the choice of roles from those that
are available for that account." An admin-only row is offered to every
approver who may delegate at least one of its grants, with only those grants;
it stays closed when none is theirs. A Card still never exceeds what its
approver holds.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import admin_only_closed
from connection_hub.delegated_credentials.cards.read_model import compatible_resource_offers
from test_resident_profile_cards import USER, _Harness

ADMIN = {"user_id": "admin-1", "roles": ["kdcube:role:super-admin"], "permissions": []}
REGISTERED = "kdcube:role:registered"
SUPER = "kdcube:role:super-admin"
SECRET = "urn:kdcube:management:secret:*"


def _connections():
    return {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": REGISTERED, "label": "Registered", "delegable_roles": [REGISTERED, SUPER]},
                    {"grant": SUPER, "label": "Super-admin", "delegable_roles": [SUPER]},
                    {"grant": "secret:write", "label": "Write secrets", "delegable_roles": [SUPER]},
                ],
                "resources": [
                    {"resource": "*", "label": "All platform and application APIs",
                     "admin_only": True, "grants": [REGISTERED, SUPER], "tools": {}},
                    {"resource": SECRET, "label": "Secrets", "admin_only": True,
                     "grants": ["secret:write"], "tools": {"write": {"grants": ["secret:write"]}}},
                ],
            }
        }
    }


def test_an_admin_only_row_is_closed_only_when_none_of_its_grants_is_the_approvers() -> None:
    row = SimpleNamespace(admin_only=True, grants=(REGISTERED, SUPER))
    assert admin_only_closed(row, {REGISTERED}, platform_admin=False) is False
    assert admin_only_closed(row, set(), platform_admin=False) is True
    assert admin_only_closed(row, set(), platform_admin=True) is False
    assert admin_only_closed(SimpleNamespace(admin_only=False, grants=()), set(), platform_admin=False) is False


@pytest.mark.asyncio
async def test_every_approver_sees_all_services_with_only_the_roles_their_account_holds(tmp_path) -> None:
    harness = _Harness(tmp_path, connections=_connections())

    mine = {row["resource"]: row for row in await harness.service.resource_options(USER, _delegable_grants=[REGISTERED])}
    assert mine["*"]["grants"] == [REGISTERED], "a non-admin chooses among their own roles only"
    assert mine["*"]["admin_only"] is False, "the admin mark shows only to a platform admin"
    assert SECRET not in mine, "a row with nothing of theirs stays closed"

    admins = {row["resource"]: row for row in await harness.service.resource_options(ADMIN, _delegable_grants=[REGISTERED, SUPER, "secret:write"])}
    assert admins["*"]["grants"] == [REGISTERED, SUPER]
    assert admins["*"]["admin_only"] is True
    assert SECRET in admins


@pytest.mark.asyncio
async def test_a_non_admin_saves_all_services_with_their_role_and_never_beyond_it(tmp_path) -> None:
    harness = _Harness(tmp_path, connections=_connections())

    created = await harness.service.create_access(USER, label="agent", resource_grants={"*": [REGISTERED]})
    assert created.get("ok") is not False, created

    refused = await harness.service.create_access(USER, label="agent", resource_grants={"*": [SUPER]})
    assert refused["ok"] is False
    assert refused["error"] == "delegated_access_grants_not_delegable"
    assert refused["grants"] == [SUPER]


def test_the_card_offer_is_open_when_the_row_carries_the_approvers_grants() -> None:
    offers = {
        offer["resource"]: offer
        for offer in compatible_resource_offers(
            card_resources=(),
            card_identity_scope="grantor",
            options=[
                {"resource": "*", "label": "All", "identity_scope": "grantor", "admin_only": True, "grants": [REGISTERED]},
                {"resource": SECRET, "label": "Secrets", "identity_scope": "grantor", "admin_only": True, "grants": []},
            ],
            platform_admin=False,
        )
    }
    assert offers["*"]["compatible"] is True
    assert offers[SECRET]["reason"] == "admin_only"

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W937: an entry-bound catalog row admits a Card only under the entry its grant named.

One published A2A agent's token must not reach another agent's door, although
both doors match the same catalog row pattern. The check sits in the shared
boundary every door calls, reads the flag from the matched row itself, and
fails closed on anything it cannot canonicalize.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.card_save_codes import CARD_SAVE_CODES
from connection_hub.delegated_credentials.catalog.authorization import ActiveCatalogCapabilities
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.entry_binding import (
    RESOURCE_OUTSIDE_ENTRY,
    canonical_entry_url,
    entry_covers,
)
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
from connection_hub.delegated_credentials.oauth.surface_policy import (
    authorize_credential_boundary,
    authorize_credential_identity_boundary,
)

PATTERN = "*/api/integrations/bundles/*/*/problem-board@1-0/public/a2a/*"
BASE = "https://hub.example"
DOOR = f"{BASE}/api/integrations/bundles/t/p/problem-board@1-0/public/a2a"
ENTRY_X = f"{DOOR}/agent-x"
OPERATIONS = ["a2a.task.send", "a2a.task.read"]


def _connections(*, entry_bound: bool = True) -> dict:
    return {
        "delegated_credentials": {
            "oauth": {
                "resources": [
                    {
                        "resource": PATTERN,
                        "grants": ["a2a:task"],
                        "entry_bound": entry_bound,
                        "tools": {name: {"grants": ["a2a:task"]} for name in OPERATIONS},
                    }
                ]
            }
        }
    }


def _catalog(*, entry_bound: bool = True) -> ActiveCatalogCapabilities:
    return ActiveCatalogCapabilities(CatalogDocument.build(_connections(entry_bound=entry_bound)))


def _envelope(operations=OPERATIONS) -> CredentialEnvelope:
    return CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:connector:card-1:user-1",
        attrs={
            "resource_grants": {PATTERN: ["a2a:task"]},
            "resource_operations": {PATTERN: list(operations)},
            "scopes": ["a2a:task"],
        },
    )


def _record(*, kind="connector", entry=ENTRY_X, operations=OPERATIONS) -> dict:
    return {
        "card_kind": kind,
        "entry_resource": entry,
        "operations": list(operations),
        "resource_operations": {PATTERN: list(operations)},
    }


def _admit(request, *, record=None, envelope=None, catalog=..., public_base=BASE):
    return authorize_credential_boundary(
        authority_id="delegated_client",
        required_roles=(),
        required_permissions=(),
        user_roles=("user",),
        user_permissions=("a2a:task",),
        envelope=envelope or _envelope(),
        grant_record=_record() if record is None else record,
        request_resource=request,
        catalog=_catalog() if catalog is ... else catalog,
        public_base=public_base,
    )


# --- canonical comparison (claude-e-app condition 2) ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("HTTPS://Hub.Example/a/B", "https://hub.example/a/B"),
        ("https://hub.example:443/a", "https://hub.example/a"),
        ("http://hub.example:80/a", "http://hub.example/a"),
        ("https://hub.example:8443/a", "https://hub.example:8443/a"),
        ("https://hub.example/a/", "https://hub.example/a"),
        ("https://hub.example/a?x=1#f", "https://hub.example/a"),
        ("https://hub.example/%61gent-x", "https://hub.example/agent-x"),
        ("https://hub.example/a%3ab", "https://hub.example/a%3Ab"),
        ("https://hub.example/v1%2E0", "https://hub.example/v1.0"),
    ],
)
def test_canonical_form(raw: str, canonical: str) -> None:
    assert canonical_entry_url(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "hub.example/a",
        "ftp://hub.example/a",
        "https://user@hub.example/a",
        "https://hub.example/a/../b",
        "https://hub.example/a/./b",
        "https://hub.example/a/%2e%2e/b",
        "https://hub.example/a/%2E/b",
        "https://hub.example/agent-x%2F..%2Fagent-y",
        "https://hub.example/a%5Cb",
        "https://hub.example/a//b",
        "https://hub.example/a%zz",
        "https://hub.example:notaport/a",
    ],
)
def test_uncanonical_urls_refuse(raw: str) -> None:
    assert canonical_entry_url(raw) is None


@pytest.mark.parametrize(
    ("request_url", "covered"),
    [
        (ENTRY_X, True),
        (ENTRY_X + "/", True),
        (ENTRY_X + "/message:send", True),
        (ENTRY_X + "?page=2", True),
        (ENTRY_X.replace("hub.example", "HUB.Example"), True),
        (ENTRY_X.replace("hub.example", "hub.example:443"), True),
        (f"{DOOR}/agent-y", False),
        (f"{DOOR}/agent-xy", False),
        (f"{ENTRY_X}/../agent-y", False),
        (f"{ENTRY_X}%2F..%2Fagent-y", False),
        (DOOR, False),
        (ENTRY_X.replace("hub.example", "evil.example"), False),
    ],
)
def test_entry_covers(request_url: str, covered: bool) -> None:
    assert entry_covers(ENTRY_X, request_url, public_base=BASE) is covered


def test_the_public_base_is_the_only_host_and_absent_fails_closed() -> None:
    other = ENTRY_X.replace("hub.example", "alias.example")
    assert not entry_covers(other, other, public_base=BASE)
    assert not entry_covers(ENTRY_X, ENTRY_X, public_base="")
    assert not entry_covers("", ENTRY_X, public_base=BASE)


# --- the boundary (conditions 1, 3, 4, 6) -----------------------------------------------------------


def test_the_catalog_row_carries_the_flag_and_it_is_inside_the_catalog_version() -> None:
    config = oauth_delegated_config_from_connections(_connections())
    (row,) = config.resources
    assert row.entry_bound is True
    unbound = CatalogDocument.build(_connections(entry_bound=False))
    bound = CatalogDocument.build(_connections(entry_bound=True))
    assert unbound.content_hash != bound.content_hash


def test_a_connector_card_is_admitted_under_its_entry() -> None:
    decision = _admit(ENTRY_X + "/message:send")
    assert decision.allowed and decision.matched_resource == PATTERN


def test_a_sibling_agents_door_is_refused() -> None:
    for request in (f"{DOOR}/agent-y", f"{DOOR}/agent-xy", f"{ENTRY_X}/../agent-y"):
        decision = _admit(request)
        assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY


@pytest.mark.parametrize("kind", ["agent", "automation", "control", ""])
def test_cards_of_other_kinds_are_refused_even_when_they_hold_the_row(kind: str) -> None:
    decision = _admit(ENTRY_X, record=_record(kind=kind))
    assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY


def test_a_card_without_an_entry_is_refused() -> None:
    decision = _admit(ENTRY_X, record=_record(entry=""))
    assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY


def test_a_record_without_the_binding_fields_fails_closed() -> None:
    # The shape KDCube's live grant record has until it carries card_kind and entry_resource.
    decision = _admit(ENTRY_X, record={"operations": OPERATIONS})
    assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY


@pytest.mark.parametrize("operations", [[], ["*"]])
def test_operations_deny_by_default(operations) -> None:
    decision = _admit(ENTRY_X, record=_record(operations=operations), envelope=_envelope(operations))
    assert not decision.allowed and decision.denial.reason == "operation_not_consented"


def test_the_request_host_must_be_the_public_base() -> None:
    decision = _admit(ENTRY_X, public_base="https://alias.example")
    assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY
    decision = _admit(ENTRY_X, public_base="")
    assert not decision.allowed and decision.denial.reason == RESOURCE_OUTSIDE_ENTRY


def test_without_a_catalog_the_boundary_refuses() -> None:
    decision = _admit(ENTRY_X, catalog=None)
    assert not decision.allowed and decision.denial.reason == "catalog_unavailable"


def test_a_row_that_is_not_entry_bound_is_unchanged() -> None:
    decision = _admit(f"{DOOR}/agent-y", catalog=_catalog(entry_bound=False), record={"operations": OPERATIONS})
    assert decision.allowed and decision.matched_resource == PATTERN


def _broader_first_catalog() -> ActiveCatalogCapabilities:
    connections = _connections()
    rows = connections["delegated_credentials"]["oauth"]["resources"]
    broader = {
        "resource": "*/api/integrations/bundles/*/*/problem-board@1-0/public/*",
        "grants": ["a2a:task"],
        "tools": {name: {"grants": ["a2a:task"]} for name in OPERATIONS},
    }
    connections["delegated_credentials"]["oauth"]["resources"] = [broader, *rows]
    return ActiveCatalogCapabilities(CatalogDocument.build(connections))


def test_a_broader_unbound_row_declared_first_does_not_unbind_the_entry() -> None:
    catalog = _broader_first_catalog()
    refused = _admit(f"{DOOR}/agent-y", catalog=catalog)
    assert not refused.allowed and refused.denial.reason == RESOURCE_OUTSIDE_ENTRY
    assert _admit(ENTRY_X + "/message:send", catalog=catalog).allowed
    automation = _admit(ENTRY_X, catalog=catalog, record=_record(kind="automation"))
    assert not automation.allowed and automation.denial.reason == RESOURCE_OUTSIDE_ENTRY


def test_no_door_can_omit_the_catalog_or_the_public_base() -> None:
    for function, names in (
        (authorize_credential_boundary, ("catalog", "public_base")),
        (authorize_credential_identity_boundary, ("catalog",)),
    ):
        parameters = inspect.signature(function).parameters
        for name in names:
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters[name].default is inspect.Parameter.empty


def test_the_proxy_door_refuses_a_card_holding_an_entry_bound_row() -> None:
    def identity(catalog):
        return authorize_credential_identity_boundary(
            authority_id="delegated_client",
            required_roles=(),
            required_permissions=(),
            user_roles=("user",),
            user_permissions=(),
            envelope=_envelope(),
            grant_record=_record(),
            catalog=catalog,
        )

    refused = identity(_catalog())
    assert not refused.allowed and refused.denial.reason == RESOURCE_OUTSIDE_ENTRY
    assert identity(_catalog(entry_bound=False)).allowed
    assert identity(None).denial.reason == "catalog_unavailable"


# --- registries (condition 5) ---------------------------------------------------------------------


def test_the_new_codes_are_registered() -> None:
    assert "card_entry_bound_row_not_selectable" in CARD_SAVE_CODES
    refused = _admit(f"{DOOR}/agent-y")
    admission = pathlib.Path(__file__).resolve().parents[3] / "apps/connection-hub@1-0/surfaces/delegated_admission.py"
    assert f'"{refused.denial.description}"' in admission.read_text(encoding="utf-8")


# --- the Card-save path (condition 5) --------------------------------------------------------------

SAVE_CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {"grant": "a2a:task", "label": "A2A tasks", "delegable_roles": ["kdcube:role:registered"]}
            ],
            "resources": [
                {
                    "resource": PATTERN,
                    "label": "A2A agents",
                    "identity_scope": "grantor",
                    "grants": ["a2a:task"],
                    "entry_bound": True,
                    "tools": {name: {"label": name, "grants": ["a2a:task"]} for name in OPERATIONS},
                },
                {"resource": "*", "label": "Other resources", "identity_scope": "grantor", "grants": ["a2a:task"]},
            ],
        }
    }
}
USER = {"user_id": "user-1", "roles": ["kdcube:role:registered"]}


def _save_service():
    from test_oauth_dcr_client_independence import _Catalog, _GrantStore, _Persistence, _Redis

    from connection_hub.delegated_credentials.automation_access import AutomationAccessService

    return AutomationAccessService(
        redis=_Redis(), tenant="t", project="p",
        config=oauth_delegated_config_from_connections(SAVE_CONNECTIONS),
        grant_store=_GrantStore({}), catalog_resolver=_Catalog(SAVE_CONNECTIONS),
        card_persistence=_Persistence(),
    )


@pytest.mark.asyncio
async def test_a_card_editor_cannot_add_an_entry_bound_row() -> None:
    service = _save_service()
    whole_card = await service.record_oauth_grant(
        grantor_subject="user-1", client_id="dcr-agent", scopes=["a2a:task"], operations=["a2a.task.send"],
        client_metadata={"kdcube_credential_use": "multi_resource"},
    )
    assert whole_card is not None and whole_card.card_kind != "connector"
    refused = await service.update_access(
        USER, access_id=whole_card.access_id,
        resource_grants={"*": ["a2a:task"], PATTERN: ["a2a:task"]},
        resource_operations={"*": [], PATTERN: ["a2a.task.send"]},
    )
    assert refused == {"ok": False, "error": "card_entry_bound_row_not_selectable", "resources": [PATTERN]}


@pytest.mark.asyncio
async def test_the_connector_card_its_grant_created_keeps_its_row_on_save() -> None:
    service = _save_service()
    connector = await service.record_oauth_grant(
        grantor_subject="user-1", client_id="dcr-a2a", scopes=["a2a:task"],
        resource_operations={ENTRY_X: ["a2a.task.send"]}, resource=ENTRY_X,
    )
    assert connector is not None and connector.card_kind == "connector"
    assert connector.entry_resource == ENTRY_X and set(connector.resource_grants) == {PATTERN}
    saved = await service.update_access(
        USER, access_id=connector.access_id,
        resource_grants={PATTERN: ["a2a:task"]},
        resource_operations={PATTERN: ["a2a.task.send", "a2a.task.read"]},
    )
    assert saved.get("error") != "card_entry_bound_row_not_selectable", saved

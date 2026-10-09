"""Application-managed grants (W661 S5): set by the owning application, never by a Card editor.

Operator, 2026-10-09: a permission that only the application's own operation may change is disabled
in the Card editor and in the client Card upsert, and the feature is generic ("cards in connection hub
do not know about PB"). A catalog resource row declares ``managed_grants``; the Hub exposes them to
the editor and refuses a client create or update that adds or removes one. A synthetic application
catalog is used here: nothing in the Hub names an application or a grant.
"""

from __future__ import annotations

import copy

import pytest

from connection_hub.delegated_credentials.catalog.descriptors import resource_row_digest
from connection_hub.delegated_credentials.managed_grants import managed_grant_changes
from connection_hub.delegated_credentials.oauth.config import oauth_delegated_config_from_connections
from test_oauth_dcr_client_independence import (
    CONCRETE_RESOURCE,
    DECLARED_RESOURCE,
    GRANTOR,
    _GrantStore,
    _Persistence,
    _service,
)

USER = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"]}


def _connections(managed=("app:steward",)):
    row = {
        "resource": DECLARED_RESOURCE,
        "label": "Synthetic application",
        "identity_scope": "grantor",
        "grants": ["app:read", "app:steward"],
        "tools": {
            "item.read": {"label": "Read", "grants": ["app:read"]},
            "item.steward": {"label": "Steward", "grants": ["app:steward"]},
        },
    }
    if managed is not None:
        row["managed_grants"] = list(managed)
    return {"delegated_credentials": {"oauth": {
        "enabled": True,
        "capabilities": [
            {"grant": "app:read", "label": "Read", "delegable_roles": ["kdcube:role:registered"]},
            {"grant": "app:steward", "label": "Steward", "delegable_roles": ["kdcube:role:registered"]},
        ],
        "resources": [row],
    }}}


def _row(connections):
    return oauth_delegated_config_from_connections(connections).resource_config(DECLARED_RESOURCE)


async def _card(connections, scopes=("app:read",), operations=("item.read",)):
    service = _service(_GrantStore({}), _Persistence(), connections=connections)
    # Seeded as the application's own write: a Card may hold a managed grant the application set.
    card = await service.record_oauth_grant(grantor_subject=GRANTOR, client_id="dcr-synthetic",
        scopes=list(scopes), operations=list(operations), resource=CONCRETE_RESOURCE, _application_write=True)
    assert card is not None
    return service, card


def test_the_catalog_declares_managed_grants_only_among_its_own_grants():
    assert _row(_connections()).managed_grants == ("app:steward",)
    assert _row(_connections(managed=None)).managed_grants == ()
    assert _row(_connections(managed=("app:steward", "app:unknown"))).managed_grants == ("app:steward",)


def test_a_row_without_managed_grants_keeps_its_digest_and_declaring_them_changes_it():
    plain, empty, managed = (_row(_connections(managed=None)), _row(_connections(managed=())),
                             _row(_connections()))
    assert resource_row_digest(plain) == resource_row_digest(empty)
    assert resource_row_digest(managed) != resource_row_digest(plain)


def test_a_change_is_any_managed_grant_whose_presence_differs():
    config = oauth_delegated_config_from_connections(_connections())
    held = {DECLARED_RESOURCE: ["app:read", "app:steward"]}
    assert managed_grant_changes(config, held, held) == []
    assert managed_grant_changes(config, {DECLARED_RESOURCE: ["app:read"]}, held) == [f"{DECLARED_RESOURCE}:app:steward"]
    assert managed_grant_changes(config, held, {DECLARED_RESOURCE: ["app:read"]}) == [f"{DECLARED_RESOURCE}:app:steward"]
    assert managed_grant_changes(config, {}, held) == [f"{DECLARED_RESOURCE}:app:steward"]
    # A manual grant moves freely.
    assert managed_grant_changes(config, {DECLARED_RESOURCE: ["app:steward"]}, held) == []
    # No managed grants declared: nothing is ever a change.
    plain = oauth_delegated_config_from_connections(_connections(managed=None))
    assert managed_grant_changes(plain, {DECLARED_RESOURCE: ["app:steward"]}, {}) == []


@pytest.mark.asyncio
async def test_a_client_update_that_ticks_or_unticks_a_managed_grant_is_refused():
    service, card = await _card(_connections())
    ticked = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert ticked["ok"] is False and ticked["error"] == "managed_grant_not_editable", ticked
    # The grant and the operation that needs it are both managed.
    assert ticked["grants"] == [f"{DECLARED_RESOURCE}:app:steward", f"{DECLARED_RESOURCE}:item.steward"]
    # A manual change on the same Card still saves.
    narrowed = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read"]}, resource_operations={DECLARED_RESOURCE: ["item.read"]})
    assert narrowed["ok"] is True, narrowed


@pytest.mark.asyncio
async def test_a_client_update_cannot_untick_a_managed_grant_the_application_set():
    service, card = await _card(_connections(), scopes=("app:read", "app:steward"),
                                operations=("item.read", "item.steward"))
    unticked = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read"]}, resource_operations={DECLARED_RESOURCE: ["item.read"]})
    assert unticked["ok"] is False and unticked["error"] == "managed_grant_not_editable", unticked
    # Keeping it as it is, while changing nothing else managed, saves.
    kept = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert kept["ok"] is True, kept


@pytest.mark.asyncio
async def test_the_application_path_still_sets_a_managed_grant():
    """Every Card write is guarded by default; only the application's own write opts out explicitly."""
    service, card = await _card(_connections())
    applied = await service.update_access(USER, access_id=card.access_id, _application_write=True,
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert applied["ok"] is True, applied
    assert set(applied["access"]["resource_grants"][DECLARED_RESOURCE]) == {"app:read", "app:steward"}


@pytest.mark.asyncio
async def test_a_client_create_with_a_managed_grant_is_refused():
    service = _service(_GrantStore({}), _Persistence(), connections=_connections())
    created = await service.create_access(USER, label="synthetic",
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert created["ok"] is False and created["error"] == "managed_grant_not_editable", created


@pytest.mark.asyncio
async def test_the_editor_receives_the_managed_grants_of_a_resource():
    service = _service(_GrantStore({}), _Persistence(), connections=_connections())
    options = await service.resource_options(USER)
    row = next(item for item in options if item["resource"] == DECLARED_RESOURCE)
    assert row["managed_grants"] == ["app:steward"]
    plain = _service(_GrantStore({}), _Persistence(), connections=_connections(managed=None))
    row = next(item for item in await plain.resource_options(USER) if item["resource"] == DECLARED_RESOURCE)
    assert "managed_grants" not in row


# W560: on a person's Control Card the application decides the operations it marks person_card: false.
def _person_connections():
    connections = _connections(managed=None)
    row = connections["delegated_credentials"]["oauth"]["resources"][0]
    row["tools"]["item.role_only"] = {"label": "Role only", "grants": ["app:read"], "person_card": False}
    return connections


def test_a_person_control_change_is_any_role_decided_operation_whose_presence_differs():
    from connection_hub.delegated_credentials.managed_grants import managed_operation_changes
    config = oauth_delegated_config_from_connections(_person_connections())
    held = {DECLARED_RESOURCE: ["item.read", "item.role_only"]}
    person = dict(person_control=True)
    assert managed_operation_changes(config, held, held, **person) == []
    assert managed_operation_changes(config, {DECLARED_RESOURCE: ["item.read"]}, held, **person) == [f"{DECLARED_RESOURCE}:item.role_only"]
    assert managed_operation_changes(config, {DECLARED_RESOURCE: ["item.read", "item.role_only"]},
                                     {DECLARED_RESOURCE: ["item.read"]}, **person) == [f"{DECLARED_RESOURCE}:item.role_only"]
    assert managed_operation_changes(config, {DECLARED_RESOURCE: ["item.steward"]}, held, **person) == [f"{DECLARED_RESOURCE}:item.role_only"]
    # Not a person's Control Card: a person_card: false operation is editable (W360).
    assert managed_operation_changes(config, {DECLARED_RESOURCE: ["item.read"]}, held) == []


@pytest.mark.asyncio
async def test_a_person_control_client_edit_cannot_tick_a_role_decided_operation_but_other_cards_can():
    service, card = await _card(_person_connections())
    person = await service.update_access(USER, access_id=card.access_id, _person_control=True,
        resource_grants={DECLARED_RESOURCE: ["app:read"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.role_only"]})
    assert person["ok"] is False and person["error"] == "managed_grant_not_editable", person
    assert person["grants"] == [f"{DECLARED_RESOURCE}:item.role_only"]
    # Not a person's Control Card: the whole catalog stays editable (W360).
    other = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.role_only"]})
    assert other["ok"] is True, other


# An operation whose required grants include a managed grant is itself managed, on every Card: the
# operation field could otherwise grant or drop that grant's power (W661 S5 / W560, approved 10:28Z).
@pytest.mark.asyncio
async def test_an_operation_needing_a_managed_grant_is_listed_managed_and_locked_on_every_card():
    from connection_hub.delegated_credentials.managed_grants import managed_operations
    config = oauth_delegated_config_from_connections(_connections())
    assert managed_operations(config.resource_config(DECLARED_RESOURCE)) == {"item.steward"}
    service, card = await _card(_connections(), scopes=("app:read", "app:steward"),
                                operations=("item.read", "item.steward"))
    row = next(item for item in await service.resource_options(USER) if item["resource"] == DECLARED_RESOURCE)
    assert [op["name"] for op in row["operations"] if op.get("managed")] == ["item.steward"]
    # Dropping the operation (keeping its grant) is refused on an ordinary Card, too.
    dropped = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read"]})
    assert dropped["ok"] is False and dropped["grants"] == [f"{DECLARED_RESOURCE}:item.steward"], dropped
    created = await _service(_GrantStore({}), _Persistence(), connections=_connections()).create_access(
        USER, label="synthetic", resource_grants={DECLARED_RESOURCE: ["app:read"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert created["ok"] is False and created["grants"] == [f"{DECLARED_RESOURCE}:item.steward"], created



# G1 (claude-test review of 97b86727): the guard is the DEFAULT of every Card write, so every client route
# (Control Card update, agent Card update, add operations, apply profile, create, update) is covered.
def _profile_connections():
    connections = _connections()
    connections["delegated_credentials"]["oauth"]["resources"][0]["authorization_profiles"] = {
        "steward": {"scope": "app:profile:steward", "label": "Steward", "operations": ["item.read", "item.steward"]},
    }
    return connections


@pytest.mark.asyncio
async def test_a_bare_card_update_is_guarded_by_default():
    service, card = await _card(_connections())
    bare = await service.update_access(USER, access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["app:read", "app:steward"]},
        resource_operations={DECLARED_RESOURCE: ["item.read", "item.steward"]})
    assert bare["ok"] is False and bare["error"] == "managed_grant_not_editable", bare


@pytest.mark.asyncio
async def test_adding_an_operation_that_needs_a_managed_grant_is_refused():
    service, card = await _card(_connections())
    added = await service.add_operations(USER, access_id=card.access_id, operations=["item.steward"])
    assert added["ok"] is False and added["error"] == "managed_grant_not_editable", added


@pytest.mark.asyncio
async def test_applying_a_profile_that_carries_a_managed_grant_is_refused():
    service, card = await _card(_profile_connections())
    applied = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="steward")
    assert applied["ok"] is False and applied["error"] == "managed_grant_not_editable", applied


def test_only_the_reset_paths_opt_out_of_the_guard():
    """A client route can only bypass the guard by passing _application_write=True; only the two Reset
    paths (which copy the Control's grants) do."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "connection_hub"
    app = pathlib.Path(__file__).resolve().parents[3] / "apps" / "connection-hub@1-0" / "entrypoint.py"
    hits = []
    for path in [*root.rglob("*.py"), app]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"_application_write=True", line):
                hits.append(path.name)
    assert sorted(hits) == ["automation_access.py", "project_person_access.py"], hits



# G2 (claude-test re-review of 225f4d33): the OAuth consent paths persist a Card directly; both are guarded.
@pytest.mark.asyncio
async def test_an_oauth_consent_requesting_a_managed_grant_is_refused_before_the_card_is_written():
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
    persistence = _Persistence()
    service = _service(_GrantStore({}), persistence, connections=_connections())
    with pytest.raises(CallerWriteRefused) as refused:
        await service.record_oauth_grant(grantor_subject=GRANTOR, client_id="dcr-consent",
            scopes=["app:read", "app:steward"], operations=["item.read", "item.steward"], resource=CONCRETE_RESOURCE)
    assert refused.value.reason == "managed_grant_not_editable"
    assert not getattr(persistence, "current", {}), "nothing was persisted"
    # The same consent without the managed grant is recorded.
    card = await service.record_oauth_grant(grantor_subject=GRANTOR, client_id="dcr-consent",
        scopes=["app:read"], operations=["item.read"], resource=CONCRETE_RESOURCE)
    assert card is not None


@pytest.mark.asyncio
async def test_a_refresh_rotation_carries_an_application_set_managed_grant_forward():
    service, card = await _card(_connections(), scopes=("app:read", "app:steward"), operations=("item.read", "item.steward"))
    rotated = await service.record_oauth_grant(grantor_subject=GRANTOR, client_id="dcr-synthetic", resource=CONCRETE_RESOURCE)
    assert rotated is not None and "app:steward" in rotated.resource_grants[DECLARED_RESOURCE]


@pytest.mark.asyncio
async def test_a_consent_extension_adding_a_managed_grant_is_refused():
    service, card = await _card(_connections())
    extended = await service.extend_client_access(USER, client_id="dcr-synthetic", access_id=card.access_id,
        resource=DECLARED_RESOURCE, claims=["app:steward"])
    assert extended["ok"] is False and extended["error"] == "managed_grant_not_editable", extended
    plain = await service.extend_client_access(USER, client_id="dcr-synthetic", access_id=card.access_id,
        resource=DECLARED_RESOURCE, claims=["app:read"])
    assert plain.get("error") != "managed_grant_not_editable", plain

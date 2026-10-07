"""W502: Reset to Control carries the Control's named-service selection for that one service.

``reset_candidate`` computed the service's named-service selection from the
Control, but ``reset_service_to_control`` did not forward it to
``update_access``, so the old selection survived the reset. This runs the real
method and ``update_access`` on the shared harness. The Control is resolved
by a stub of ``_compose_with_control``, the hierarchy resolver's output.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.cards.model import ControlCardBinding, NamedServiceSelection
from test_resident_profile_cards import (
    CLIENT,
    MEMORIES,
    NAMED_SERVICES,
    NAMED_SERVICES_CONCRETE,
    USER,
    _connections_with_named_services,
    _Harness,
)


@pytest.mark.asyncio
async def test_reset_to_control_applies_the_controls_named_services_and_keeps_other_services(tmp_path):
    h = _Harness(tmp_path, connections=_connections_with_named_services())
    created = await h.service.create_access(
        USER, label="lg-react",
        resource_grants={NAMED_SERVICES_CONCRETE: ["named_services:use", "slack:read"],
                         MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]},
        named_service_operations={NAMED_SERVICES_CONCRETE: {}},
        client_id=CLIENT,
    )
    assert created["ok"], created
    access_id = created["access"]["access_id"]
    authority, handles = h.persistence.cards[access_id]
    bound = dataclasses.replace(authority, control_card=ControlCardBinding(
        control_id="control-person", issuer_ref="work:project:one", issuer_kind="project", control_revision=1))
    h.persistence.cards[access_id] = (bound, handles)
    existing = await h.service._load_record(access_id, grantor_subject=bound.grantor_subject)

    control = dataclasses.replace(
        existing, access_id="control-person", control_card=None,
        resource_grants={NAMED_SERVICES: ("named_services:use", "slack:read")},
        resource_operations={NAMED_SERVICES: ()},
        named_service_operations=NamedServiceSelection.exact({NAMED_SERVICES: {"slack": ["object.search"]}}))

    async def compose_with_control(_record):
        return control, None

    h.service._compose_with_control = compose_with_control
    result = await h.service.reset_service_to_control(
        USER, access_id=access_id, resource=NAMED_SERVICES, expected_card_revision=existing.card_revision)
    assert result["ok"], result
    after = result["access"]
    assert after["named_service_operations"] == {NAMED_SERVICES: {"slack": ["object.search"]}}  # the Control's
    assert after["resource_grants"][MEMORIES] == ["memories:read"]  # an unrelated service is untouched
    assert after["resource_operations"][MEMORIES] == ["search"]


@pytest.mark.asyncio
async def test_a_reset_that_would_leave_nothing_selected_refuses_and_never_revokes(tmp_path):
    """Operator: a Card edit never invalidates a delivered token. A reset emptying the Card is refused, not a revoke."""
    h = _Harness(tmp_path, connections=_connections_with_named_services())
    created = await h.service.create_access(
        USER, label="lg-react", resource_grants={MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]}, client_id=CLIENT)
    assert created["ok"], created
    access_id = created["access"]["access_id"]
    authority, handles = h.persistence.cards[access_id]
    bound = dataclasses.replace(authority, control_card=ControlCardBinding(
        control_id="control-person", issuer_ref="work:project:one", issuer_kind="project", control_revision=1))
    h.persistence.cards[access_id] = (bound, handles)
    existing = await h.service._load_record(access_id, grantor_subject=bound.grantor_subject)
    control = dataclasses.replace(existing, access_id="control-person", control_card=None,
                                  resource_grants={MEMORIES: ()}, resource_operations={MEMORIES: ()})

    async def compose_with_control(_record):
        return control, None

    h.service._compose_with_control = compose_with_control
    result = await h.service.reset_service_to_control(
        USER, access_id=access_id, resource=MEMORIES, expected_card_revision=existing.card_revision)
    assert result["ok"] is False, result
    stored, _ = h.persistence.cards[access_id]
    assert stored.state == "active"  # not revoked: the delivered credential keeps working
    assert stored.card_revision == existing.card_revision  # nothing written

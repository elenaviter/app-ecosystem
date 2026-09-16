# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_OPERATIONS_PROPERTY,
    ApplicationOperationPolicyError,
    ApplicationOperationRolePolicy,
    application_operation_role_policy,
    compose_application_operation_role_policy,
    validate_application_operation_role_policy,
)
from connection_hub.delegated_credentials.cards.model import (
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
    ControlCardBinding,
)
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.model import (
    new_credentialless_card,
)


REGISTERED = "kdcube:role:registered"
PRIVILEGED = "kdcube:role:privileged"
SUPER_ADMIN = "kdcube:role:super-admin"
OP_READ = "urn:kdcube:application-operation:example%401-0:read"
OP_WRITE = "urn:kdcube:application-operation:example%401-0:write"
OP_ADMIN = "urn:kdcube:application-operation:example%401-0:admin"


def _property(
    default_role: str,
    operation_roles: dict[str, str] | None = None,
) -> dict:
    return {
        APPLICATION_OPERATIONS_PROPERTY: {
            "schema": "kdcube.application_operations.v2",
            "mode": "selected",
            "default_role": default_role,
            "operation_roles": operation_roles or {},
        }
    }


def _card(
    *,
    access_id: str,
    default_role: str,
    operations: tuple[str, ...],
    operation_roles: dict[str, str] | None = None,
    binding: ControlCardBinding | None = None,
    policy: bool = True,
) -> CardAuthority:
    return CardAuthority(
        access_id=access_id,
        client_id=f"client:{access_id}",
        grantor_subject="user-1",
        delegate_subject=f"delegate:{access_id}",
        source="agent",
        card_revision=1,
        catalog_version="catalog-v1",
        resource_grants={"*": (default_role,)},
        resource_operations={"*": operations},
        control_card=binding,
        properties=_property(default_role, operation_roles) if policy else {},
    )


def test_v1_policy_infers_one_strongest_historical_default_role() -> None:
    policy = application_operation_role_policy(
        {
            APPLICATION_OPERATIONS_PROPERTY: {
                "schema": "kdcube.application_operations.v1",
                "mode": "selected",
            }
        },
        resource_grants={"*": [REGISTERED, SUPER_ADMIN]},
    )

    assert policy is not None
    assert policy.default_role == SUPER_ADMIN
    assert policy.operation_roles == {}


def test_v2_policy_rejects_an_override_for_an_unselected_operation() -> None:
    policy = application_operation_role_policy(
        _property(REGISTERED, {OP_ADMIN: SUPER_ADMIN}),
        resource_grants={"*": [REGISTERED]},
    )

    assert policy is not None
    with pytest.raises(
        ApplicationOperationPolicyError,
        match="application_operation_override_not_selected",
    ):
        validate_application_operation_role_policy(
            policy,
            selected_operations=(OP_READ,),
        )


def test_v2_policy_downscopes_beneath_one_configured_role_ceiling() -> None:
    policy = application_operation_role_policy(
        _property(REGISTERED, {OP_ADMIN: SUPER_ADMIN}),
        resource_grants={"*": [REGISTERED]},
    )

    assert policy is not None
    validate_application_operation_role_policy(
        policy,
        selected_operations=(OP_READ, OP_ADMIN),
        delegable_roles=(REGISTERED, SUPER_ADMIN),
        allowed_roles=(SUPER_ADMIN,),
    )


def test_v2_policy_cannot_raise_above_the_configured_role_ceiling() -> None:
    policy = application_operation_role_policy(
        _property(REGISTERED, {OP_ADMIN: SUPER_ADMIN}),
        resource_grants={"*": [REGISTERED]},
    )

    assert policy is not None
    with pytest.raises(
        ApplicationOperationPolicyError,
        match="application_roles_not_allowed_for_resource",
    ):
        validate_application_operation_role_policy(
            policy,
            selected_operations=(OP_READ, OP_ADMIN),
            delegable_roles=(REGISTERED, SUPER_ADMIN),
            allowed_roles=(REGISTERED,),
        )


def test_and_and_or_compose_each_selected_operation_role() -> None:
    caller = ApplicationOperationRolePolicy(
        default_role=REGISTERED,
        operation_roles={OP_ADMIN: SUPER_ADMIN},
    )
    control = ApplicationOperationRolePolicy(
        default_role=PRIVILEGED,
        operation_roles={OP_WRITE: REGISTERED},
    )

    narrowed, narrowed_operations = compose_application_operation_role_policy(
        caller,
        control,
        card_operations=(OP_READ, OP_WRITE, OP_ADMIN),
        control_operations=(OP_READ, OP_WRITE),
        composition_mode=CONTROL_COMPOSITION_AND,
    )
    contributed, contributed_operations = compose_application_operation_role_policy(
        caller,
        control,
        card_operations=(OP_READ, OP_ADMIN),
        control_operations=(OP_READ, OP_WRITE),
        composition_mode=CONTROL_COMPOSITION_OR,
    )

    assert narrowed.default_role == REGISTERED
    assert narrowed_operations == (OP_READ, OP_WRITE)
    assert narrowed.role_for(OP_READ) == REGISTERED
    assert narrowed.role_for(OP_WRITE) == REGISTERED
    assert contributed.default_role == PRIVILEGED
    assert contributed_operations == (OP_ADMIN, OP_READ, OP_WRITE)
    assert contributed.role_for(OP_READ) == PRIVILEGED
    assert contributed.role_for(OP_WRITE) == REGISTERED
    assert contributed.role_for(OP_ADMIN) == SUPER_ADMIN


@pytest.mark.parametrize(
    ("mode", "expected_default", "expected_admin"),
    [
        (CONTROL_COMPOSITION_AND, REGISTERED, PRIVILEGED),
        (CONTROL_COMPOSITION_OR, PRIVILEGED, SUPER_ADMIN),
    ],
)
def test_effective_card_encodes_the_same_composed_mapping_it_enforces(
    mode: str,
    expected_default: str,
    expected_admin: str,
) -> None:
    caller = _card(
        access_id="caller",
        default_role=REGISTERED,
        operations=(OP_READ, OP_ADMIN),
        operation_roles={OP_ADMIN: SUPER_ADMIN},
    )
    control_seed = _card(
        access_id="seed",
        default_role=PRIVILEGED,
        operations=(OP_READ, OP_ADMIN),
    )
    control = new_credentialless_card(
        initial_selection=control_seed,
        grantor_subject="user-1",
        catalog_version="catalog-v1",
        control_id="control-1",
        issuer_ref="work:project:example",
        issuer_kind="application",
        composition_mode=mode,
    )
    bound = dataclasses.replace(
        caller,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )

    effective = effective_card_authority(bound, control)
    policy = application_operation_role_policy(
        effective.properties,
        resource_grants=effective.resource_grants,
    )

    assert policy is not None
    assert effective.resource_grants["*"] == (expected_default,)
    assert policy.default_role == expected_default
    assert policy.role_for(OP_ADMIN) == expected_admin
    assert effective.resource_operations["*"] == (OP_ADMIN, OP_READ)


@pytest.mark.parametrize("control_operations", [(), (OP_READ,)])
def test_pre_policy_caller_is_narrowed_by_v2_control_in_and_mode(
    control_operations: tuple[str, ...],
) -> None:
    caller = _card(
        access_id="pre-policy-caller",
        default_role=SUPER_ADMIN,
        operations=(),
        policy=False,
    )
    control_seed = _card(
        access_id="control-seed",
        default_role=REGISTERED,
        operations=control_operations,
    )
    control = new_credentialless_card(
        initial_selection=control_seed,
        grantor_subject="user-1",
        catalog_version="catalog-v2",
        control_id="control-v2",
        issuer_ref="work:project:example",
        issuer_kind="application",
        composition_mode=CONTROL_COMPOSITION_AND,
    )
    bound = dataclasses.replace(
        caller,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )

    effective = effective_card_authority(bound, control)
    policy = application_operation_role_policy(
        effective.properties,
        resource_grants=effective.resource_grants,
    )

    assert policy is not None
    assert policy.default_role == REGISTERED
    assert effective.resource_grants["*"] == (REGISTERED,)
    assert effective.resource_operations["*"] == control_operations


def test_inert_policy_key_without_application_resource_is_not_a_gate() -> None:
    caller = _card(
        access_id="pre-policy-caller",
        default_role=REGISTERED,
        operations=(OP_READ,),
        policy=False,
    )
    control_seed = dataclasses.replace(
        _card(
            access_id="control-seed",
            default_role=SUPER_ADMIN,
            operations=(),
            policy=False,
        ),
        resource_grants={},
        resource_operations={},
        properties={APPLICATION_OPERATIONS_PROPERTY: {"written_by": "system"}},
    )
    control = new_credentialless_card(
        initial_selection=control_seed,
        grantor_subject="user-1",
        catalog_version="catalog-v2",
        control_id="control-with-inert-marker",
        issuer_ref="work:project:example",
        issuer_kind="application",
        composition_mode=CONTROL_COMPOSITION_OR,
    )
    bound = dataclasses.replace(
        caller,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=control.card_revision,
        ),
    )

    effective = effective_card_authority(bound, control)

    assert effective.resource_grants["*"] == (REGISTERED,)
    assert effective.resource_operations["*"] == (OP_READ,)
    assert APPLICATION_OPERATIONS_PROPERTY not in effective.properties

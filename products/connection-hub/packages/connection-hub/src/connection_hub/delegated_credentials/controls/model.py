# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Control Card linking helpers plus the legacy project-card decoder."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from connection_hub.agent_account_scope import (
    normalize_account_scope,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CREDENTIALLESS_CARD_SOURCE,
    CONTROL_COMPOSITION_AND,
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONTROL
from connection_hub.delegated_credentials.catalog.drift import (
    selected_named_service_operations,
)
from connection_hub.delegated_credentials.named_service_policy import (
    as_string_list,
    clean_text,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_operations,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    materialize_control_snapshot,
)

CONTROL_CARD_SCHEMA = "connection_hub.project_control_card.v1"
CONTROL_CARD_STATE_ACTIVE = "active"
CONTROL_CARD_STATE_RETIRED = "retired"


class ProjectControlCardError(ValueError):
    """A Control Card record or relationship is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


ControlCardError = ProjectControlCardError


def control_card_id_for_issuer(
    issuer_kind: str,
    issuer_ref: str,
    *,
    grantor_subject: str,
) -> str:
    """Stable idempotent Card id for one issuer-owned control definition."""
    kind = clean_text(issuer_kind)
    reference = clean_text(issuer_ref)
    grantor = clean_text(grantor_subject)
    if not kind:
        raise ControlCardError("control_card_issuer_kind_missing")
    if not reference:
        raise ControlCardError("control_card_issuer_ref_missing")
    if not grantor:
        raise ControlCardError("control_card_grantor_missing")
    digest = hashlib.sha256(
        f"{grantor}\0{kind}\0{reference}".encode("utf-8")
    ).hexdigest()[:24]
    return f"control-{digest}"


def control_card_id_for_project(project_ref: str) -> str:
    """Legacy deterministic id retained only while old PB rows migrate."""
    value = clean_text(project_ref)
    if not value:
        raise ProjectControlCardError("project_ref_missing")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"project-control-{digest}"


def new_credentialless_card(
    *,
    grantor_subject: str,
    catalog_version: str,
    initial_selection: CardAuthority | None = None,
    control_id: str,
    issuer_ref: str,
    issuer_kind: str,
    issuer_label: str = "",
    manage_url: str = "",
    properties: Mapping[str, Any] | None = None,
    composition_mode: str = CONTROL_COMPOSITION_AND,
    revision: int = 1,
    now: int = 0,
) -> CardAuthority:
    """Create a regular Card without credential handles or expiry.

    ``initial_selection`` supplies only the initially checked values. The
    Control Card's editor and every later save use the catalog, so this Card is
    never bounded by the source Card.
    """

    seed = initial_selection
    selected = selected_named_service_operations(seed) if seed is not None else {}
    provenance: dict[str, Any] = {}
    if seed is not None:
        provenance["control_card_initial_selection"] = {
            "access_id": seed.access_id,
            "card_revision": seed.card_revision,
            "catalog_version": seed.catalog_version,
        }
    selected_properties = dict(seed.properties or {}) if seed is not None else {}
    selected_properties.update(dict(properties or {}))
    result = CardAuthority(
        access_id=clean_text(control_id),
        client_id=f"control-card:{clean_text(issuer_kind)}",
        grantor_subject=clean_text(grantor_subject),
        delegate_subject="",
        source=CREDENTIALLESS_CARD_SOURCE,
        card_kind=CARD_KIND_CONTROL,
        label=clean_text(issuer_label) or clean_text(issuer_ref),
        card_revision=max(1, int(revision)),
        catalog_version=clean_text(catalog_version),
        state=CARD_STATE_ACTIVE,
        resource_grants={
            resource: tuple(grants)
            for resource, grants in (seed.resource_grants.items() if seed else ())
        },
        resource_operations={
            resource: tuple(operations)
            for resource, operations in (seed.resource_operations.items() if seed else ())
        },
        named_service_operations=NamedServiceSelection.exact(
            {
                resource: {
                    namespace: sorted(operations)
                    for namespace, operations in namespaces.items()
                }
                for resource, namespaces in selected.items()
            }
        ),
        named_services=dict(seed.named_services or {}) if seed is not None else {},
        account_scope=(
            normalize_account_scope(seed.account_scope) if seed is not None else {}
        ),
        identity_scope=(
            clean_text(seed.identity_scope) if seed is not None else ""
        )
        or "grantor",
        created_at=max(0, int(now)),
        expires_at=0,
        resource_acceptance=(
            dict(seed.resource_acceptance or {}) if seed is not None else {}
        ),
        provenance=provenance,
        issuer_ref=clean_text(issuer_ref),
        issuer_kind=clean_text(issuer_kind),
        issuer_label=clean_text(issuer_label),
        manage_url=clean_text(manage_url),
        composition_mode=clean_text(composition_mode).lower() or CONTROL_COMPOSITION_AND,
        properties=selected_properties,
    )
    if not result.access_id:
        raise ControlCardError("control_card_id_missing")
    if not result.grantor_subject:
        raise ControlCardError("control_card_grantor_missing")
    if not result.catalog_version:
        raise ControlCardError("control_card_catalog_version_missing")
    return materialize_control_snapshot(
        result,
        basis_catalog_version=result.catalog_version,
        origin="created",
        source_card_revision=(seed.card_revision if seed is not None else None),
    )


def control_card_from_legacy(
    authority: "ProjectControlCardAuthority",
    *,
    properties: Mapping[str, Any] | None = None,
) -> CardAuthority:
    """Adapt an old PB-owned projection during the bounded migration window."""

    card = CardAuthority(
        access_id=authority.control_id,
        client_id=f"control-card:{authority.issuer_kind}",
        grantor_subject=authority.grantor_subject,
        delegate_subject="",
        source="control",
        card_kind=CARD_KIND_CONTROL,
        label=authority.issuer_label or authority.issuer_ref,
        card_revision=authority.revision,
        catalog_version=authority.basis_catalog_version,
        state=(
            CARD_STATE_ACTIVE
            if authority.state == CONTROL_CARD_STATE_ACTIVE
            else "revoked"
        ),
        resource_grants=authority.resource_grants,
        resource_operations=authority.resource_operations,
        named_service_operations=authority.named_service_operations,
        account_scope=authority.account_scope,
        created_at=authority.created_at,
        expires_at=0,
        provenance={
            "legacy_project_control": {
                "basis_access_id": authority.basis_access_id,
                "basis_card_revision": authority.basis_card_revision,
                "basis_catalog_version": authority.basis_catalog_version,
            }
        },
        issuer_ref=authority.issuer_ref,
        issuer_kind=authority.issuer_kind,
        issuer_label=authority.issuer_label,
        manage_url=authority.manage_url,
        composition_mode=CONTROL_COMPOSITION_AND,
        properties=dict(properties or {}),
    )
    return materialize_control_snapshot(
        card,
        basis_catalog_version=authority.basis_catalog_version,
        origin="legacy_project_control",
        source_card_revision=authority.revision,
    )


def _resource_grants(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ProjectControlCardError("resource_grants_invalid")
    return {
        clean_text(resource): tuple(as_string_list(grants))
        for resource, grants in value.items()
        if clean_text(resource)
    }


def _named_selection(value: Any, *, present: bool) -> NamedServiceSelection:
    try:
        selection = NamedServiceSelection.from_stored(value, present=present)
    except Exception as exc:
        raise ProjectControlCardError("named_service_operations_invalid") from exc
    if selection.is_unknown:
        raise ProjectControlCardError("named_service_operations_missing")
    return selection


def _string_set_subset(candidate: tuple[str, ...], maximum: tuple[str, ...]) -> bool:
    held = set(maximum)
    return "*" in held or set(candidate).issubset(held)


def _named_map(selection: NamedServiceSelection) -> dict[str, dict[str, set[str]]]:
    if selection.is_none:
        return {}
    if selection.is_all:
        return {"*": {"*": {"*"}}}
    return {
        resource: {
            namespace: set(operations)
            for namespace, operations in namespaces.items()
        }
        for resource, namespaces in selection.operations.items()
    }


def _named_subset(
    candidate: NamedServiceSelection,
    maximum: NamedServiceSelection,
) -> bool:
    if maximum.is_all:
        return True
    if candidate.is_all:
        return False
    candidate_map = _named_map(candidate)
    maximum_map = _named_map(maximum)
    for resource, namespaces in candidate_map.items():
        held_namespaces = maximum_map.get(resource)
        if held_namespaces is None:
            return False
        for namespace, operations in namespaces.items():
            if not operations.issubset(held_namespaces.get(namespace, set())):
                return False
    return True


def _account_subset(
    candidate: Mapping[str, Mapping[str, tuple[str, ...]]],
    maximum: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> bool:
    for provider, accounts in candidate.items():
        maximum_accounts = maximum.get(provider) or maximum.get("*")
        if not maximum_accounts:
            return False
        for account_id, claims in accounts.items():
            held = maximum_accounts.get(account_id) or maximum_accounts.get("*")
            if held is None or not _string_set_subset(claims, tuple(held)):
                return False
    return True


@dataclass(frozen=True)
class ProjectControlCardAuthority:
    """The permissions one project permits its participants to exercise.

    It carries no credential. A delegated Card references it, and live
    admission intersects both objects for every operation.
    """

    control_id: str
    issuer_ref: str
    grantor_subject: str
    basis_access_id: str
    revision: int
    basis_card_revision: int = 0
    basis_catalog_version: str = ""
    resource_grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    resource_operations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    named_service_operations: NamedServiceSelection = field(
        default_factory=NamedServiceSelection.none
    )
    account_scope: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    issuer_kind: str = "project"
    issuer_label: str = ""
    manage_url: str = ""
    state: str = CONTROL_CARD_STATE_ACTIVE
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_card(
        cls,
        card: CardAuthority,
        *,
        control_id: str,
        issuer_ref: str,
        issuer_label: str = "",
        manage_url: str = "",
        revision: int = 1,
        now: int = 0,
    ) -> "ProjectControlCardAuthority":
        selected = selected_named_service_operations(card)
        result = cls(
            control_id=control_id,
            issuer_ref=issuer_ref,
            issuer_label=issuer_label,
            manage_url=manage_url,
            grantor_subject=card.grantor_subject,
            basis_access_id=card.access_id,
            basis_card_revision=card.card_revision,
            basis_catalog_version=card.catalog_version,
            revision=revision,
            resource_grants={
                resource: tuple(grants)
                for resource, grants in card.resource_grants.items()
            },
            resource_operations={
                resource: tuple(operations)
                for resource, operations in card.resource_operations.items()
            },
            named_service_operations=NamedServiceSelection.exact(
                {
                    resource: {
                        namespace: sorted(operations)
                        for namespace, operations in namespaces.items()
                    }
                    for resource, namespaces in selected.items()
                }
            ),
            account_scope=normalize_account_scope(card.account_scope),
            created_at=now,
            updated_at=now,
        )
        result.validate()
        return result

    @classmethod
    def from_mapping(cls, value: Any) -> "ProjectControlCardAuthority":
        if not isinstance(value, Mapping):
            raise ProjectControlCardError("control_card_not_object")
        if clean_text(value.get("schema")) != CONTROL_CARD_SCHEMA:
            raise ProjectControlCardError("control_card_schema_mismatch")
        state = clean_text(value.get("state")) or CONTROL_CARD_STATE_ACTIVE
        if state not in {CONTROL_CARD_STATE_ACTIVE, CONTROL_CARD_STATE_RETIRED}:
            raise ProjectControlCardError("control_card_state_invalid")
        try:
            operations = normalize_resource_operations(value.get("resource_operations"))
        except Exception as exc:
            raise ProjectControlCardError("resource_operations_invalid") from exc
        try:
            revision = int(value.get("revision") or 0)
            basis_card_revision = int(value.get("basis_card_revision") or 0)
            created_at = int(value.get("created_at") or 0)
            updated_at = int(value.get("updated_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ProjectControlCardError("control_card_number_invalid") from exc
        result = cls(
            control_id=clean_text(value.get("control_id")),
            issuer_ref=clean_text(value.get("issuer_ref")),
            issuer_kind=clean_text(value.get("issuer_kind")) or "project",
            issuer_label=clean_text(value.get("issuer_label")),
            manage_url=clean_text(value.get("manage_url")),
            grantor_subject=clean_text(value.get("grantor_subject")),
            basis_access_id=clean_text(value.get("basis_access_id")),
            basis_card_revision=basis_card_revision,
            basis_catalog_version=clean_text(value.get("basis_catalog_version")),
            revision=revision,
            state=state,
            resource_grants=_resource_grants(value.get("resource_grants")),
            resource_operations=operations,
            named_service_operations=_named_selection(
                value.get("named_service_operations"),
                present="named_service_operations" in value,
            ),
            account_scope=normalize_account_scope(
                value.get("account_scope")
                if isinstance(value.get("account_scope"), Mapping)
                else {}
            ),
            created_at=created_at,
            updated_at=updated_at,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not self.control_id:
            raise ProjectControlCardError("control_card_id_missing")
        if not self.issuer_ref:
            raise ProjectControlCardError("control_card_issuer_ref_missing")
        if self.issuer_kind != "project":
            raise ProjectControlCardError("control_card_issuer_kind_invalid")
        if not self.grantor_subject:
            raise ProjectControlCardError("control_card_grantor_missing")
        if not self.basis_access_id:
            raise ProjectControlCardError("control_card_basis_missing")
        if self.revision < 1:
            raise ProjectControlCardError("control_card_revision_invalid")
        if self.basis_card_revision < 0:
            raise ProjectControlCardError("control_card_basis_revision_invalid")
        unknown = set(self.resource_operations) - set(self.resource_grants)
        if unknown:
            raise ProjectControlCardError("control_card_operation_resource_invalid")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": CONTROL_CARD_SCHEMA,
            "control_id": self.control_id,
            "issuer_ref": self.issuer_ref,
            "issuer_kind": self.issuer_kind,
            "issuer_label": self.issuer_label,
            "manage_url": self.manage_url,
            "grantor_subject": self.grantor_subject,
            "basis_access_id": self.basis_access_id,
            "basis_card_revision": self.basis_card_revision,
            "basis_catalog_version": self.basis_catalog_version,
            "revision": self.revision,
            "state": self.state,
            "resource_grants": {
                resource: list(grants)
                for resource, grants in self.resource_grants.items()
            },
            "resource_operations": {
                resource: list(operations)
                for resource, operations in self.resource_operations.items()
            },
            "account_scope": {
                provider: {
                    account_id: list(claims)
                    for account_id, claims in accounts.items()
                }
                for provider, accounts in self.account_scope.items()
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        stored = self.named_service_operations.to_stored()
        payload["named_service_operations"] = {} if stored is None else stored
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_dict()


def control_card_is_subset(
    candidate: ProjectControlCardAuthority,
    maximum: ProjectControlCardAuthority,
) -> bool:
    """Compare two legacy project-control records during staged migration."""

    try:
        candidate.validate()
        maximum.validate()
    except ProjectControlCardError:
        return False
    if (
        candidate.control_id != maximum.control_id
        or candidate.issuer_ref != maximum.issuer_ref
        or candidate.grantor_subject != maximum.grantor_subject
        or candidate.basis_access_id != maximum.basis_access_id
        or candidate.basis_card_revision != maximum.basis_card_revision
        or candidate.basis_catalog_version != maximum.basis_catalog_version
    ):
        return False
    for resource, grants in candidate.resource_grants.items():
        held = maximum.resource_grants.get(resource)
        if held is None or not _string_set_subset(tuple(grants), tuple(held)):
            return False
    for resource, operations in candidate.resource_operations.items():
        held = maximum.resource_operations.get(resource)
        if held is None or not set(operations).issubset(held):
            return False
    return _named_subset(
        candidate.named_service_operations,
        maximum.named_service_operations,
    ) and _account_subset(candidate.account_scope, maximum.account_scope)


__all__ = [
    "CONTROL_CARD_SCHEMA",
    "CONTROL_CARD_STATE_ACTIVE",
    "CONTROL_CARD_STATE_RETIRED",
    "ControlCardError",
    "ProjectControlCardAuthority",
    "ProjectControlCardError",
    "control_card_from_legacy",
    "control_card_id_for_issuer",
    "control_card_id_for_project",
    "control_card_is_subset",
    "new_credentialless_card",
]

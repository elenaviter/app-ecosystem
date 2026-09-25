# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable project identity edge and person-owned My Card lifecycle."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AUTOMATION,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    PROJECT_PERSON_SELECTION_SOURCE,
    CardAuthority,
    ControlCardBinding,
)
from connection_hub.delegated_credentials.cards.service import replace_state
from connection_hub.delegated_credentials.catalog.authorization import (
    ActiveCatalogCapabilities,
)
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.named_service_policy import clean_text
from connection_hub.delegated_credentials.project_identity_authorization import (
    PROJECT_IDENTITY_EDGE_SCHEMA,
    ProjectCardResolution,
    ProjectIdentityCardReference,
    ProjectIdentityDelegationEdge,
    ProjectOperationAuthorizationDecision,
    ProjectOperationRequest,
    authorize_project_operation,
)


PROJECT_IDENTITY_EDGE_PROVENANCE = "project_identity_edge"
PROJECT_PERSON_MY_CARD_ISSUER_KIND = "project-person"
PROJECT_PERSON_MY_CARD_CLIENT_PREFIX = "kdcube-project-person:"


AuthorityFromRecord = Callable[[Any], CardAuthority]
RecordFromAuthority = Callable[[CardAuthority], Any]


class ProjectIdentityLifecycleError(ValueError):
    """A project identity edge or companion My Card is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required(value: Any, reason: str) -> str:
    result = clean_text(value)
    if not result:
        raise ProjectIdentityLifecycleError(reason)
    return result


def _revision(value: Any, reason: str) -> int:
    try:
        revision = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ProjectIdentityLifecycleError(reason) from exc
    if revision < 1:
        raise ProjectIdentityLifecycleError(reason)
    return revision


def _digest(project_ref: str, person_subject: str) -> str:
    return hashlib.sha256(
        f"{project_ref}\0{person_subject}".encode("utf-8")
    ).hexdigest()[:24]


@dataclass(frozen=True)
class ProjectPersonCardIdentity:
    """Stable project/person coordinates shared by the edge and My Card."""

    project_ref: str
    person_subject: str
    project_subject: str
    control_id: str
    my_card_id: str
    client_id: str
    edge_ref: str
    initial_control_revision: int = 1
    initial_my_card_revision: int = 1

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        person_subject: Any,
        control_revision: Any = 1,
        my_card_revision: Any = 1,
    ) -> "ProjectPersonCardIdentity":
        project = _required(project_ref, "project_identity_project_ref_missing")
        person = _required(person_subject, "project_identity_person_subject_missing")
        control = ProjectPersonControlIdentity.build(
            project_ref=project,
            target_subject=person,
        )
        digest = _digest(project, person)
        return cls(
            project_ref=project,
            person_subject=person,
            project_subject=control.project_subject,
            control_id=control.control_id,
            my_card_id=f"person-my-card-{digest}",
            client_id=f"{PROJECT_PERSON_MY_CARD_CLIENT_PREFIX}{digest}",
            edge_ref=f"project-person-edge-{digest}",
            initial_control_revision=_revision(
                control_revision,
                "project_identity_control_revision_missing",
            ),
            initial_my_card_revision=_revision(
                my_card_revision,
                "project_identity_my_card_revision_missing",
            ),
        )

    @classmethod
    def from_my_card(cls, authority: CardAuthority) -> "ProjectPersonCardIdentity":
        raw = dict(authority.provenance or {}).get(PROJECT_IDENTITY_EDGE_PROVENANCE)
        if not isinstance(raw, Mapping):
            raise ProjectIdentityLifecycleError("project_identity_edge_marker_missing")
        if clean_text(raw.get("schema")) != PROJECT_IDENTITY_EDGE_SCHEMA:
            raise ProjectIdentityLifecycleError("project_identity_edge_schema_invalid")
        person = raw.get("person_identity")
        project = raw.get("project_identity")
        cards = raw.get("cards")
        if not isinstance(person, Mapping) or not isinstance(project, Mapping):
            raise ProjectIdentityLifecycleError("project_identity_edge_marker_invalid")
        if not isinstance(cards, Mapping):
            raise ProjectIdentityLifecycleError("project_identity_edge_marker_invalid")
        control = cards.get("control_card")
        my_card = cards.get("my_card")
        if not isinstance(control, Mapping) or not isinstance(my_card, Mapping):
            raise ProjectIdentityLifecycleError("project_identity_edge_marker_invalid")
        identity = cls.build(
            project_ref=project.get("ref"),
            person_subject=person.get("subject"),
            control_revision=control.get("card_revision"),
            my_card_revision=my_card.get("card_revision"),
        )
        expected = {
            "project_subject": identity.project_subject,
            "control_id": identity.control_id,
            "my_card_id": identity.my_card_id,
            "edge_ref": identity.edge_ref,
        }
        actual = {
            "project_subject": clean_text(project.get("subject")),
            "control_id": clean_text(control.get("access_id")),
            "my_card_id": clean_text(my_card.get("access_id")),
            "edge_ref": clean_text(raw.get("edge_ref")),
        }
        if actual != expected:
            raise ProjectIdentityLifecycleError("project_identity_edge_marker_mismatch")
        identity.validate_my_card(authority)
        return identity

    def marker(self) -> dict[str, Any]:
        return {
            "schema": PROJECT_IDENTITY_EDGE_SCHEMA,
            "edge_ref": self.edge_ref,
            "person_identity": {"subject": self.person_subject},
            "project_identity": {
                "ref": self.project_ref,
                "subject": self.project_subject,
            },
            "cards": {
                "control_card": {
                    "access_id": self.control_id,
                    "grantor_subject": self.project_subject,
                    "card_revision": self.initial_control_revision,
                    "issuer_ref": self.project_ref,
                    "issuer_kind": PROJECT_PERSON_CONTROL_ISSUER_KIND,
                },
                "my_card": {
                    "access_id": self.my_card_id,
                    "grantor_subject": self.person_subject,
                    "card_revision": self.initial_my_card_revision,
                    "issuer_ref": self.project_ref,
                    "issuer_kind": PROJECT_PERSON_MY_CARD_ISSUER_KIND,
                },
            },
        }

    def validate_my_card(self, authority: CardAuthority) -> None:
        binding = authority.control_card
        mismatches = (
            (
                authority.source != PROJECT_PERSON_SELECTION_SOURCE,
                "project_identity_my_card_source_invalid",
            ),
            (
                authority.card_kind != CARD_KIND_AUTOMATION,
                "project_identity_my_card_kind_invalid",
            ),
            (
                authority.access_id != self.my_card_id,
                "project_identity_my_card_id_mismatch",
            ),
            (
                authority.grantor_subject != self.person_subject
                or authority.delegate_subject != self.person_subject,
                "project_identity_my_card_owner_mismatch",
            ),
            (
                authority.issuer_ref != self.project_ref
                or authority.issuer_kind != PROJECT_PERSON_MY_CARD_ISSUER_KIND,
                "project_identity_my_card_issuer_mismatch",
            ),
            (
                binding is None
                or binding.control_id != self.control_id
                or binding.issuer_ref != self.project_ref
                or binding.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND,
                "project_identity_my_card_control_binding_mismatch",
            ),
        )
        reason = next((reason for condition, reason in mismatches if condition), "")
        if reason:
            raise ProjectIdentityLifecycleError(reason)

    def edge(
        self,
        *,
        control_card: CardAuthority | None,
        my_card: CardAuthority | None,
    ) -> ProjectIdentityDelegationEdge:
        control_revision = (
            control_card.card_revision
            if control_card is not None
            else self.initial_control_revision
        )
        my_revision = (
            my_card.card_revision
            if my_card is not None
            else self.initial_my_card_revision
        )
        return ProjectIdentityDelegationEdge(
            edge_ref=self.edge_ref,
            person_subject=self.person_subject,
            project_ref=self.project_ref,
            project_subject=self.project_subject,
            control_card=ProjectIdentityCardReference(
                access_id=self.control_id,
                grantor_subject=self.project_subject,
                card_revision=control_revision,
                issuer_ref=self.project_ref,
                issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
            ),
            my_card=ProjectIdentityCardReference(
                access_id=self.my_card_id,
                grantor_subject=self.person_subject,
                card_revision=my_revision,
                issuer_ref=self.project_ref,
                issuer_kind=PROJECT_PERSON_MY_CARD_ISSUER_KIND,
            ),
        )


def new_project_person_my_card(
    *,
    control_card: CardAuthority,
    initial_selection: CardAuthority | None = None,
    initial_provenance: Mapping[str, Any] | None = None,
    label: str = "",
    manage_url: str = "",
    now: int | None = None,
) -> CardAuthority:
    """Create the person's positive selection under one Control Card.

    Every project-person Card starts equal to its Control Card. The person may
    narrow that positive selection afterward; the Control Card and active
    catalog remain ceilings at every operation.
    """

    control_identity = ProjectPersonControlIdentity.from_authority(control_card)
    identity = ProjectPersonCardIdentity.build(
        project_ref=control_identity.project_ref,
        person_subject=control_identity.target_subject,
        control_revision=control_card.card_revision,
    )
    seed = initial_selection or control_card
    seed_identity = ProjectPersonControlIdentity.from_authority(seed)
    if (
        seed_identity.project_ref != control_identity.project_ref
        or seed_identity.target_subject != control_identity.target_subject
        or seed.access_id != control_card.access_id
        or seed.card_revision != control_card.card_revision
    ):
        raise ProjectIdentityLifecycleError(
            "project_identity_initial_selection_mismatch"
        )
    provenance = {PROJECT_IDENTITY_EDGE_PROVENANCE: identity.marker()}
    for key, value in dict(initial_provenance or {}).items():
        marker_key = clean_text(key)
        if not marker_key or marker_key == PROJECT_IDENTITY_EDGE_PROVENANCE:
            raise ProjectIdentityLifecycleError(
                "project_identity_initial_provenance_invalid"
            )
        provenance[marker_key] = copy.deepcopy(value)
    created_at = int(time.time()) if now is None else int(now)
    authority = CardAuthority(
        access_id=identity.my_card_id,
        client_id=identity.client_id,
        grantor_subject=identity.person_subject,
        delegate_subject=identity.person_subject,
        source=PROJECT_PERSON_SELECTION_SOURCE,
        card_kind=CARD_KIND_AUTOMATION,
        label=clean_text(label) or "My Card",
        card_revision=identity.initial_my_card_revision,
        catalog_version=control_card.catalog_version,
        state=CARD_STATE_ACTIVE,
        operations=tuple(seed.operations),
        resource_grants={
            resource: tuple(grants)
            for resource, grants in seed.resource_grants.items()
        },
        resource_operations={
            resource: tuple(operations)
            for resource, operations in seed.resource_operations.items()
        },
        named_service_operations=seed.named_service_operations,
        named_services=copy.deepcopy(dict(seed.named_services)),
        account_scope={
            provider: {
                account_id: tuple(claims) for account_id, claims in accounts.items()
            }
            for provider, accounts in seed.account_scope.items()
        },
        identity_scope="grantor",
        created_at=created_at,
        expires_at=0,
        resource_acceptance=copy.deepcopy(dict(seed.resource_acceptance)),
        provenance=provenance,
        control_card=ControlCardBinding(
            control_id=identity.control_id,
            issuer_ref=identity.project_ref,
            issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
            issuer_label=control_card.issuer_label or control_card.label,
            manage_url=control_card.manage_url,
            control_revision=control_card.card_revision,
        ),
        issuer_ref=identity.project_ref,
        issuer_kind=PROJECT_PERSON_MY_CARD_ISSUER_KIND,
        issuer_label=control_card.issuer_label or control_card.label,
        manage_url=clean_text(manage_url),
    )
    identity.validate_my_card(authority)
    return authority


@dataclass(frozen=True)
class ProjectIdentityLifecycleResult:
    edge: ProjectIdentityDelegationEdge
    my_card: Any
    my_card_created: bool


@dataclass(frozen=True)
class ProjectIdentityResolution:
    edge: ProjectIdentityDelegationEdge | None
    control_card: ProjectCardResolution
    my_card: ProjectCardResolution


class ProjectIdentityLifecycle:
    """Persist and resolve the cross-owner project identity relationship."""

    def __init__(
        self,
        *,
        host: Any,
        authority_from_record: AuthorityFromRecord,
        record_from_authority: RecordFromAuthority,
    ) -> None:
        self._host = host
        self._authority_from_record = authority_from_record
        self._record_from_authority = record_from_authority

    def _authority(self, record: Any, state: str) -> CardAuthority:
        return dataclasses.replace(self._authority_from_record(record), state=state)

    async def ensure(
        self,
        control_record: Any,
        *,
        initial_selection: CardAuthority | None = None,
        initial_provenance: Mapping[str, Any] | None = None,
    ) -> ProjectIdentityLifecycleResult:
        control = self._authority_from_record(control_record)
        control_identity = ProjectPersonControlIdentity.from_authority(control)
        identity = ProjectPersonCardIdentity.build(
            project_ref=control_identity.project_ref,
            person_subject=control_identity.target_subject,
            control_revision=control.card_revision,
        )
        loaded = await self._host._load_record_any_state(
            identity.my_card_id,
            grantor_subject=identity.person_subject,
        )
        created = False
        if loaded is None:
            my_authority = new_project_person_my_card(
                control_card=control,
                initial_selection=initial_selection,
                initial_provenance=initial_provenance,
            )
            my_record = self._record_from_authority(my_authority)
            await self._host._persist_record(my_record, expected_revision=0)
            state = CARD_STATE_ACTIVE
            created = True
        else:
            my_record, state = loaded
            my_authority = self._authority(my_record, state)
            if state != CARD_STATE_ACTIVE:
                raise ProjectIdentityLifecycleError(
                    "project_identity_my_card_not_active"
                )
            existing_identity = ProjectPersonCardIdentity.from_my_card(my_authority)
            if existing_identity.edge_ref != identity.edge_ref:
                raise ProjectIdentityLifecycleError("project_identity_edge_conflict")
            for key, value in dict(initial_provenance or {}).items():
                if my_authority.provenance.get(key) != value:
                    raise ProjectIdentityLifecycleError(
                        "project_identity_initialization_conflict"
                    )
        my_authority = self._authority(my_record, state)
        identity = ProjectPersonCardIdentity.from_my_card(my_authority)
        edge = identity.edge(control_card=control, my_card=my_authority)
        if reason := edge.validation_reason():
            raise ProjectIdentityLifecycleError(reason)
        return ProjectIdentityLifecycleResult(
            edge=edge,
            my_card=my_record,
            my_card_created=created,
        )

    async def resolve(
        self,
        *,
        project_ref: str,
        person_subject: str,
    ) -> ProjectIdentityResolution:
        expected = ProjectPersonCardIdentity.build(
            project_ref=project_ref,
            person_subject=person_subject,
        )
        loaded_my = await self._host._load_record_any_state(
            expected.my_card_id,
            grantor_subject=expected.person_subject,
        )
        if loaded_my is None:
            return ProjectIdentityResolution(
                edge=None,
                control_card=ProjectCardResolution.missing(),
                my_card=ProjectCardResolution.missing(),
            )
        my_record, my_state = loaded_my
        my_authority = self._authority(my_record, my_state)
        identity = ProjectPersonCardIdentity.from_my_card(my_authority)
        if identity.edge_ref != expected.edge_ref:
            raise ProjectIdentityLifecycleError("project_identity_edge_conflict")

        loaded_control = await self._host._load_record_any_state(
            identity.control_id,
            grantor_subject=identity.project_subject,
        )
        control_authority: CardAuthority | None = None
        control_resolution = ProjectCardResolution.missing()
        if loaded_control is not None:
            control_record, control_state = loaded_control
            control_authority = self._authority(control_record, control_state)
            control_resolution = ProjectCardResolution.current(control_authority)
        edge = identity.edge(
            control_card=control_authority,
            my_card=my_authority,
        )
        if reason := edge.validation_reason():
            raise ProjectIdentityLifecycleError(reason)
        return ProjectIdentityResolution(
            edge=edge,
            control_card=control_resolution,
            my_card=ProjectCardResolution.current(my_authority),
        )

    async def end(
        self,
        *,
        project_ref: str,
        person_subject: str,
    ) -> bool:
        identity = ProjectPersonCardIdentity.build(
            project_ref=project_ref,
            person_subject=person_subject,
        )
        loaded = await self._host._load_record_any_state(
            identity.my_card_id,
            grantor_subject=identity.person_subject,
        )
        if loaded is None:
            return False
        record, state = loaded
        authority = self._authority(record, state)
        ProjectPersonCardIdentity.from_my_card(authority)
        if state == CARD_STATE_REVOKED:
            return False
        if state != CARD_STATE_ACTIVE:
            raise ProjectIdentityLifecycleError("project_identity_my_card_not_active")
        revoked = replace_state(authority, CARD_STATE_REVOKED)
        await self._host._forget_record(
            record,
            revoked_record=self._record_from_authority(revoked),
        )
        return True

    async def authorize(
        self,
        request: ProjectOperationRequest,
    ) -> ProjectOperationAuthorizationDecision:
        if request.validation_reason():
            return authorize_project_operation(
                request=request,
                edge=None,
                control_card=None,
                my_card=None,
                catalog=None,
            )
        resolution = await self.resolve(
            project_ref=request.project_ref,
            person_subject=request.person_subject,
        )
        catalog: ActiveCatalogCapabilities | None
        try:
            catalog = ActiveCatalogCapabilities(await self._host._active_catalog())
        except CatalogUnavailable:
            catalog = None
        return authorize_project_operation(
            request=request,
            edge=resolution.edge,
            control_card=resolution.control_card,
            my_card=resolution.my_card,
            catalog=catalog,
        )


__all__ = [
    "PROJECT_IDENTITY_EDGE_PROVENANCE",
    "PROJECT_PERSON_MY_CARD_CLIENT_PREFIX",
    "PROJECT_PERSON_MY_CARD_ISSUER_KIND",
    "ProjectIdentityLifecycle",
    "ProjectIdentityLifecycleError",
    "ProjectIdentityLifecycleResult",
    "ProjectIdentityResolution",
    "ProjectPersonCardIdentity",
    "new_project_person_my_card",
]

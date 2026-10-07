# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Read-only proposals for a group of project-person Cards.

The builders in this module are also used by the ordinary create paths.  They
take already resolved catalog selections and construct durable Card authority;
neither builder persists a Card or issues a credential.
"""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any, Callable, Mapping, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards.card_group import (
    MAX_GROUP_MEMBERS,
    group_candidate_value,
    group_member,
    hub_group_participant_input,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CardAuthority,
    ControlCardBinding,
)
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.cards.service import replace_state
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.descriptors import next_resource_acceptance
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlAudit,
    ProjectPersonControlIdentity,
    ProjectPersonControlError,
    bind_project_person_control,
)
from connection_hub.delegated_credentials.controls.snapshot import materialize_control_snapshot
from connection_hub.delegated_credentials.project_authorization import (
    LifecyclePlanAuthorization,
    LifecyclePlanAuthorizationRequest,
    LifecyclePlanStep,
    PROJECT_PERSON_CONTROL_REVOKE,
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
)
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import (
    CREATION_STEPS,
    UPDATE_STEPS,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    PROJECT_PERSON_MY_CARD_ISSUER_KIND,
    ProjectIdentityLifecycleError,
    ProjectPersonCardIdentity,
    new_project_person_my_card,
)
from connection_hub.delegated_credentials.controls.project_invitation import (
    ProjectInvitationControlError,
    ProjectInvitationControlIdentity,
)
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer


def removed_person_card(original: CardAuthority, *, project_ref: str, target_subject: str,
                        decision: ProjectAuthorizationDecision, actor_subject: str, request_id: str,
                        now: int) -> tuple[str, CardAuthority]:
    """W502: one active person Card of ``target_subject`` in ``project_ref``, revoked; nothing else changes.

    Returns ``("control" | "my", candidate)``. The person Control keeps its
    identity binding and records the same "revoked" audit the direct removal
    writes; the My Card keeps every selection and preference, only its state
    moves. Any other Card, project or person refuses.
    """
    if (not decision.allowed or decision.operation != PROJECT_PERSON_CONTROL_REVOKE
            or decision.project_ref != project_ref or decision.target_subject != target_subject):
        raise CardLifecyclePlanRefused("card_plan_authorization_invalid", 403)
    if original.state != CARD_STATE_ACTIVE:
        raise CardLifecyclePlanRefused("card_plan_revoke_not_active")
    if original.issuer_kind == PROJECT_PERSON_CONTROL_ISSUER_KIND:
        try:
            identity = ProjectPersonControlIdentity.from_authority(original)
        except ProjectPersonControlError:
            raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403) from None
        if identity.project_ref != project_ref or identity.target_subject != target_subject:
            raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403)
        revoked = bind_project_person_control(replace_state(original, CARD_STATE_REVOKED), identity=identity)
        audit = ProjectPersonControlAudit.build(
            action="revoked", actor_subject=actor_subject, identity=identity, request_id=request_id,
            occurred_at=now, before=original, after=revoked)
        return "control", bind_project_person_control(revoked, identity=identity, audit=audit)
    if original.issuer_kind == PROJECT_PERSON_MY_CARD_ISSUER_KIND:
        try:
            identity = ProjectPersonCardIdentity.from_my_card(original)
        except ProjectIdentityLifecycleError:
            raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403) from None
        if identity.project_ref != project_ref or identity.person_subject != target_subject:
            raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403)
        return "my", replace_state(original, CARD_STATE_REVOKED)
    raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403)


def _with_selection(
    authority: CardAuthority,
    *,
    resolved: Any,
    catalog_version: str,
    resource_row_for: Callable[[str], Any],
) -> CardAuthority:
    """Apply the one catalog resolution used by live and planned constructors."""
    if resolved.error is not None or resolved.revoke:
        raise ValueError("control_selection_unresolved")
    grants = {key: tuple(value) for key, value in resolved.resource_grants.items()}
    operations = {key: tuple(value) for key, value in resolved.resource_operations.items()}
    return dataclasses.replace(
        authority,
        operations=tuple(resolved.operations),
        resource_grants=grants,
        resource_operations=operations,
        named_service_operations=resolved.named_service_operations,
        named_services=copy.deepcopy(resolved.named_services),
        account_scope={
            provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
            for provider, accounts in resolved.account_scope.items()
        },
        identity_scope=resolved.identity_scope,
        properties=copy.deepcopy(resolved.properties),
        resource_acceptance=next_resource_acceptance(
            resources=resolved.resource_grants,
            row_for=resource_row_for,
            catalog_version=catalog_version,
            selected_operations=resolved.resource_operations,
        ),
    )


def build_application_control(
    *,
    project_ref: str,
    holder_subject: str,
    catalog_version: str,
    issuer_kind: str = "application",
    issuer_label: str = "",
    manage_url: str = "",
    properties: Mapping[str, Any] | None = None,
    composition_mode: str = "and",
    initial_selection: CardAuthority | None = None,
    profile_marker: Mapping[str, Any] | None = None,
    base: CardAuthority | None = None,
    resolved: Any = None,
    resource_row_for: Callable[[str], Any] | None = None,
    now: int = 0,
) -> CardAuthority:
    """Build an issuer-owned project Control Card without a write or bearer."""
    from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer

    authority = base if base is not None else new_credentialless_card(
        control_id=control_card_id_for_issuer(
            issuer_kind, project_ref, grantor_subject=holder_subject
        ),
        grantor_subject=holder_subject,
        catalog_version=catalog_version,
        initial_selection=initial_selection,
        issuer_ref=project_ref,
        issuer_kind=issuer_kind,
        issuer_label=issuer_label,
        manage_url=manage_url,
        properties=properties,
        composition_mode=composition_mode,
        now=now,
    )
    if profile_marker is not None and base is None:
        authority = dataclasses.replace(
            authority,
            provenance={
                **dict(authority.provenance or {}),
                "control_card_initial_selection": copy.deepcopy(dict(profile_marker)),
            },
        )
    if resolved is not None:
        if resource_row_for is None:
            raise ValueError("resource_row_for_required")
        authority = _with_selection(
            authority,
            resolved=resolved,
            catalog_version=catalog_version,
            resource_row_for=resource_row_for,
        )
    return materialize_control_snapshot(
        authority, basis_catalog_version=catalog_version, origin="created"
    )


def build_project_person_control(
    *,
    identity: ProjectPersonControlIdentity,
    catalog_version: str,
    actor_subject: str,
    request_id: str,
    label: str = "",
    manage_url: str = "",
    properties: Mapping[str, Any] | None = None,
    composition_mode: str = "and",
    base: CardAuthority | None = None,
    resolved: Any = None,
    resource_row_for: Callable[[str], Any] | None = None,
    seed_origin: tuple[str, str] | None = None,
    parent: CardAuthority | None = None,
    now: int = 0,
    audit_at: int | None = None,
    finalize: bool = True,
) -> CardAuthority:
    """Build C with the same selection, audit and origin as live creation.

    ``finalize=False`` provides the unselected first record to the catalog
    resolver; it is never a proposed Card.  A planned parent is bound in the
    first revision, while the existing live path verifies and binds its P.
    """
    authority = base if base is not None else bind_project_person_control(
        new_credentialless_card(
            control_id=identity.control_id,
            grantor_subject=identity.project_subject,
            catalog_version=catalog_version,
            issuer_ref=identity.project_ref,
            issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
            issuer_label=label or identity.target_subject,
            manage_url=manage_url,
            properties=properties,
            composition_mode=composition_mode,
            now=now,
        ),
        identity=identity,
    )
    if resolved is not None:
        if resource_row_for is None:
            raise ValueError("resource_row_for_required")
        authority = _with_selection(
            authority,
            resolved=resolved,
            catalog_version=catalog_version,
            resource_row_for=resource_row_for,
        )
    if not finalize:
        return authority
    authority = bind_project_person_control(
        materialize_control_snapshot(
            authority, basis_catalog_version=catalog_version, origin="created"
        ),
        identity=identity,
    )
    occurred_at = now if audit_at is None else audit_at
    audit = ProjectPersonControlAudit.build(
        action="created",
        actor_subject=actor_subject,
        identity=identity,
        request_id=request_id,
        occurred_at=occurred_at,
        before=None,
        after=authority,
    )
    authority = bind_project_person_control(authority, identity=identity, audit=audit)
    if seed_origin is not None:
        provenance_key, provenance_schema = seed_origin
        provenance = copy.deepcopy(dict(authority.provenance or {}))
        provenance[provenance_key] = {
            "schema": provenance_schema,
            "actor_subject": actor_subject,
            "request_id": request_id,
            "created_at": audit.occurred_at,
        }
        authority = dataclasses.replace(authority, provenance=provenance)
    if parent is not None:
        authority = dataclasses.replace(
            authority,
            control_card=ControlCardBinding(
                control_id=parent.access_id,
                issuer_ref=parent.issuer_ref,
                issuer_kind=parent.issuer_kind,
                issuer_label=parent.issuer_label,
                manage_url=parent.manage_url,
                control_revision=parent.card_revision,
                holder_subject=parent.grantor_subject,
            ),
        )
    return authority


class CardLifecyclePlanRefused(ValueError):
    """An invalid or stale proposal; no Card has been changed."""

    def __init__(self, reason: str, status: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _required_text(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CardLifecyclePlanRefused(reason, 400)
    return value


def _parent_binding(parent: CardAuthority) -> ControlCardBinding:
    return ControlCardBinding(
        control_id=parent.access_id,
        issuer_ref=parent.issuer_ref,
        issuer_kind=parent.issuer_kind,
        issuer_label=parent.issuer_label,
        manage_url=parent.manage_url,
        control_revision=parent.card_revision,
        holder_subject=parent.grantor_subject,
    )


def _require_project_parent(
    parent: CardAuthority, *, project_ref: str, authorization: ProjectAuthorizationDecision,
    planned_parent: bool,
) -> None:
    """A request may name P, but cannot turn an arbitrary Control into P."""
    expected_id = control_card_id_for_issuer(
        "application", project_ref, grantor_subject=parent.grantor_subject,
    )
    if (parent.state != CARD_STATE_ACTIVE or parent.access_id != expected_id
            or parent.issuer_kind != "application" or parent.issuer_ref != project_ref):
        raise CardLifecyclePlanRefused("project_control_not_exact")
    if parent.control_card is not None:
        raise CardLifecyclePlanRefused("project_control_not_root")
    locator = authorization.project_control
    if locator is None and not planned_parent:
        raise CardLifecyclePlanRefused("project_control_locator_missing")
    if locator is not None and (locator.control_id != parent.access_id
                                or locator.holder_subject != parent.grantor_subject):
        raise CardLifecyclePlanRefused("project_control_locator_mismatch")


async def plan_card_lifecycle(
    host: Any,
    *,
    project_ref: str,
    creations: Sequence[Mapping[str, Any]],
    updates: Sequence[Mapping[str, Any]] = (),
    actor_subject: str,
    actor_kind: str,
    request_id: str,
    authorization: LifecyclePlanAuthorization,
) -> dict[str, Any]:
    """Propose a canonical Card group using only current reads and catalog resolution.

    The caller owns project policy and the signed proposal. This function
    checks its scope, builds exact durable Card values, and returns the Hub's
    participant input. The Card service checks all dependencies again at
    prepare; planning takes no lock and writes nothing.
    """
    try:
        scope = _required_text(project_ref, "card_plan_project_ref_invalid")
        actor = _required_text(actor_subject, "card_plan_actor_invalid")
        _required_text(request_id, "card_plan_request_id_invalid")
        if actor_kind not in ("caller", "grantor"):
            raise CardLifecyclePlanRefused("card_plan_actor_invalid", 400)
        if (not isinstance(creations, (list, tuple)) or not isinstance(updates, (list, tuple))
                or not creations and not updates):
            raise CardLifecyclePlanRefused("card_plan_empty", 400)
        if len(creations) + len(updates) > MAX_GROUP_MEMBERS:
            raise CardLifecyclePlanRefused("card_group_too_large", 400)

        # The signed operation owns the complete request digest. Reconstruct
        # its exact step list without inventing a weaker digest from these
        # planner arguments, then bind the host's envelope to every step.
        if not isinstance(authorization, LifecyclePlanAuthorization):
            raise CardLifecyclePlanRefused("card_plan_authorization_invalid", 403)
        steps: list[LifecyclePlanStep] = []
        for raw in creations:
            if not isinstance(raw, Mapping) or raw.get("kind") not in CREATION_STEPS:
                raise CardLifecyclePlanRefused("card_plan_creation_invalid", 400)
            identity = raw.get("identity")
            if not isinstance(identity, Mapping):
                raise CardLifecyclePlanRefused("card_plan_creation_identity_invalid", 400)
            operation, target_field = CREATION_STEPS[raw["kind"]]
            steps.append(LifecyclePlanStep(
                ref=_required_text(raw.get("ref"), "card_plan_creation_ref_invalid"),
                operation=operation,
                target_subject=_required_text(identity.get(target_field), "card_plan_target_invalid"),
            ))
        for index, raw in enumerate(updates):
            if not isinstance(raw, Mapping) or raw.get("kind") not in UPDATE_STEPS:
                raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
            steps.append(LifecyclePlanStep(
                ref=f"update:{index}", operation=UPDATE_STEPS[raw["kind"]],
                target_subject=_required_text(raw.get("target_subject"), "card_plan_target_invalid"),
            ))
        try:
            authorization.validate_for(LifecyclePlanAuthorizationRequest(
                actor_subject=actor, project_ref=scope, request_id=request_id,
                request_digest=authorization.request.request_digest, steps=tuple(steps),
            ))
        except ProjectAuthorizationError as exc:
            raise CardLifecyclePlanRefused("card_plan_authorization_invalid", 403) from exc
        if not authorization.allowed:
            raise CardLifecyclePlanRefused(authorization.refusal(), 403)

        active = await host._active_catalog()
        catalog_version = host._version_of(active)
        content_hash = _required_text(
            getattr(active, "content_hash", ""), "card_plan_catalog_hash_missing"
        )
        digest = catalog_version_digest(catalog_version, content_hash)
        cards = host._cards()
        now = int(time.time())
        planned: dict[str, CardAuthority] = {}
        members: list[dict[str, Any]] = []
        # W607: each member's exact original (None for a creation), for the public before/after display.
        originals: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        reads_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        live_cache: dict[tuple[str, str], CardAuthority | None] = {}
        row_config: dict[str, Any] = {}

        async def load_live(subject_hash: str, access_id: str) -> CardAuthority | None:
            key = (subject_hash, access_id)
            if key not in live_cache:
                loaded = await cards.load_current(access_id, subject_hash=subject_hash)
                live_cache[key] = loaded[0] if loaded is not None else None
            authority = live_cache[key]
            if authority is not None:
                reads_by_key[key] = {
                    "subject_hash": subject_hash,
                    "access_id": access_id,
                    "revision": authority.card_revision,
                }
            return authority

        async def parent_for(raw: Any) -> CardAuthority | None:
            if raw is None:
                return None
            if not isinstance(raw, Mapping):
                raise CardLifecyclePlanRefused("card_plan_parent_invalid", 400)
            if set(raw) == {"ref"}:
                ref = _required_text(raw["ref"], "card_plan_parent_invalid")
                if ref not in planned:
                    raise CardLifecyclePlanRefused("card_plan_parent_unresolved")
                return planned[ref]
            if set(raw) != {"access_id", "holder_subject"}:
                raise CardLifecyclePlanRefused("card_plan_parent_invalid", 400)
            access_id = _required_text(raw["access_id"], "card_plan_parent_invalid")
            holder = _required_text(raw["holder_subject"], "card_plan_parent_invalid")
            parent = await load_live(subject_hash_for(holder), access_id)
            if parent is None or parent.state != CARD_STATE_ACTIVE:
                raise CardLifecyclePlanRefused("card_plan_parent_not_active")
            return parent

        # The proposer may list children before parents; construct in a
        # topological order, then the group helper sorts by storage key.
        pending: dict[str, Mapping[str, Any]] = {}
        for raw in creations:
            if not isinstance(raw, Mapping) or set(raw) != {
                "ref", "kind", "identity", "selection", "parent"
            }:
                raise CardLifecyclePlanRefused("card_plan_creation_invalid", 400)
            ref = _required_text(raw["ref"], "card_plan_creation_ref_invalid")
            if ref in pending:
                raise CardLifecyclePlanRefused("card_plan_creation_ref_duplicate", 400)
            if not isinstance(raw["identity"], Mapping) or not isinstance(raw["selection"], Mapping):
                raise CardLifecyclePlanRefused("card_plan_creation_invalid", 400)
            if set(raw["selection"]) - {
                "resource_grants", "resource_operations", "named_service_operations",
                "account_scope", "properties",
            }:
                raise CardLifecyclePlanRefused("card_plan_selection_invalid", 400)
            if "properties" in raw["selection"] and not isinstance(raw["selection"]["properties"], Mapping):
                raise CardLifecyclePlanRefused("card_plan_selection_invalid", 400)
            identity_fields = {
                "application_control": {"holder_subject", "issuer_ref", "issuer_label", "manage_url", "composition_mode"},
                "project_person_control": {"target_subject", "label", "manage_url", "composition_mode", "seed_origin"},
                "project_person_my_card": {"person_subject", "label", "manage_url"},
            }
            if raw["kind"] not in identity_fields or set(raw["identity"]) - identity_fields[raw["kind"]]:
                raise CardLifecyclePlanRefused("card_plan_creation_identity_invalid", 400)
            pending[ref] = raw

        while pending:
            progressed = False
            for ref, raw in tuple(pending.items()):
                parent_raw = raw["parent"]
                if (isinstance(parent_raw, Mapping) and set(parent_raw) == {"ref"}
                        and parent_raw["ref"] in pending):
                    continue
                kind = raw["kind"]
                decision = authorization.decision_for(ref)
                identity = raw["identity"]
                selection = raw["selection"]
                parent = await parent_for(parent_raw)
                selected = any(key in selection for key in (
                    "resource_grants", "resource_operations",
                    "named_service_operations", "account_scope",
                ))
                resolved = None
                if kind == "application_control":
                    holder = _required_text(identity.get("holder_subject"), "card_plan_holder_invalid")
                    if holder != decision.target_subject:
                        raise CardLifecyclePlanRefused("card_plan_authorization_target_mismatch", 403)
                    if identity.get("issuer_ref") != scope or parent is not None:
                        raise CardLifecyclePlanRefused("card_plan_application_identity_invalid", 400)
                    if identity.get("composition_mode", "and") not in ("and", "or"):
                        raise CardLifecyclePlanRefused("control_card_composition_mode_invalid", 400)
                    issuer_label = str(identity.get("issuer_label") or "")
                    manage_url = str(identity.get("manage_url") or "")
                    mode = str(identity.get("composition_mode") or "and")
                    base = build_application_control(
                        project_ref=scope, holder_subject=holder, catalog_version=catalog_version,
                        issuer_label=issuer_label, manage_url=manage_url,
                        composition_mode=mode, properties=selection.get("properties"), now=now,
                    )
                    if selected:
                        from connection_hub.delegated_credentials.automation_access import record_from_card
                        resolved = await host._resolve_card_authority(
                            user={"user_id": holder}, existing=record_from_card(base), active=active,
                            resource_grants=selection.get("resource_grants") or {},
                            resource_operations=selection.get("resource_operations"), operations=(),
                            named_service_operations=selection.get("named_service_operations"),
                            account_scope=selection.get("account_scope"), properties=base.properties,
                            _delegable_grants=decision.delegable_grants,
                            _platform_admin=decision.platform_admin,
                        )
                        if resolved.error is not None:
                            return dict(resolved.error)
                        if resolved.revoke:
                            return {"ok": False, "error": "control_card_initial_selection_empty", "status": 409,
                                    "pruned": resolved.reconciled.to_public_dict()}
                        row_config[holder] = await host._catalog_config(active, owner_subject=holder)
                        base = build_application_control(
                            project_ref=scope, holder_subject=holder, catalog_version=catalog_version,
                            base=base, resolved=resolved,
                            resource_row_for=lambda resource, owner=holder: host._configured_resource(
                                resource, config=row_config[owner]
                            ),
                        )
                elif kind == "project_person_control":
                    if parent is None:
                        raise CardLifecyclePlanRefused("card_plan_project_control_missing")
                    target = _required_text(identity.get("target_subject"), "card_plan_target_invalid")
                    if target != decision.target_subject:
                        raise CardLifecyclePlanRefused("card_plan_authorization_target_mismatch", 403)
                    _require_project_parent(
                        parent, project_ref=scope, authorization=decision,
                        planned_parent=isinstance(parent_raw, Mapping) and set(parent_raw) == {"ref"},
                    )
                    person = ProjectPersonControlIdentity.build(project_ref=scope, target_subject=target)
                    if identity.get("composition_mode", "and") not in ("and", "or"):
                        raise CardLifecyclePlanRefused("control_card_composition_mode_invalid", 400)
                    seed_name = identity.get("seed_origin")
                    seed_origin = None
                    if seed_name:
                        from connection_hub.delegated_credentials.project_person_access import (
                            PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE,
                            PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA,
                            PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE,
                            PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA,
                        )
                        seed_origins = {
                            "migration": (PROJECT_PERSON_CONTROL_MIGRATION_PROVENANCE,
                                          PROJECT_PERSON_CONTROL_MIGRATION_SCHEMA),
                            "project_creation": (PROJECT_PERSON_CONTROL_PROJECT_CREATION_PROVENANCE,
                                                 PROJECT_PERSON_CONTROL_PROJECT_CREATION_SCHEMA),
                        }
                        if seed_name not in seed_origins:
                            raise CardLifecyclePlanRefused("card_plan_seed_origin_invalid", 400)
                        seed_origin = seed_origins[seed_name]
                    mode = str(identity.get("composition_mode") or "and")
                    base = build_project_person_control(
                        identity=person, catalog_version=catalog_version,
                        actor_subject=actor, request_id=request_id,
                        label=str(identity.get("label") or ""),
                        manage_url=str(identity.get("manage_url") or ""),
                        composition_mode=mode, properties=selection.get("properties"),
                        now=now, finalize=False,
                    )
                    if selected:
                        from connection_hub.delegated_credentials.automation_access import record_from_card
                        from connection_hub.delegated_credentials.project_person_access import _named_not_delegable
                        resolved = await host._resolve_card_authority(
                            user={"user_id": person.project_subject}, existing=record_from_card(base),
                            active=active, resource_grants=selection.get("resource_grants") or {},
                            resource_operations=selection.get("resource_operations"), operations=(),
                            named_service_operations=selection.get("named_service_operations"),
                            account_scope=selection.get("account_scope"), properties=base.properties,
                            _delegable_grants=decision.delegable_grants,
                            _platform_admin=decision.platform_admin,
                        )
                        if resolved.error is not None:
                            return _named_not_delegable(resolved.error, decision)
                        if resolved.revoke:
                            return {"ok": False, "error": "project_person_control_selection_empty", "status": 409,
                                    "pruned": resolved.reconciled.to_public_dict()}
                        row_config[person.project_subject] = await host._catalog_config(
                            active, owner_subject=person.project_subject
                        )
                    base = build_project_person_control(
                        identity=person, catalog_version=catalog_version, actor_subject=actor,
                        request_id=request_id, base=base, resolved=resolved,
                        resource_row_for=(
                            lambda resource, owner=person.project_subject: host._configured_resource(
                                resource, config=row_config[owner]
                            )
                        ) if resolved is not None else None,
                        seed_origin=seed_origin, parent=parent, now=now, audit_at=now,
                    )
                elif kind == "project_person_my_card":
                    target = _required_text(identity.get("person_subject"), "card_plan_target_invalid")
                    if target != decision.target_subject or parent is None:
                        raise CardLifecyclePlanRefused("card_plan_my_parent_invalid")
                    try:
                        parent_identity = ProjectPersonControlIdentity.from_authority(parent)
                    except ProjectPersonControlError as exc:
                        raise CardLifecyclePlanRefused("card_plan_my_parent_invalid") from exc
                    if (parent_identity.project_ref != scope
                            or parent_identity.target_subject != target):
                        raise CardLifecyclePlanRefused("card_plan_my_parent_invalid")
                    base = new_project_person_my_card(
                        control_card=parent, label=str(identity.get("label") or ""),
                        manage_url=str(identity.get("manage_url") or ""), now=now,
                    )
                    if selected:
                        from connection_hub.delegated_credentials.automation_access import record_from_card
                        resolved = await host._resolve_card_authority(
                            user={"user_id": target}, existing=record_from_card(base), active=active,
                            resource_grants=selection.get("resource_grants") or {},
                            resource_operations=selection.get("resource_operations"), operations=(),
                            named_service_operations=selection.get("named_service_operations"),
                            account_scope=selection.get("account_scope"), properties=base.properties,
                            _delegable_grants=decision.delegable_grants,
                            _platform_admin=decision.platform_admin,
                        )
                        if resolved.error is not None:
                            return dict(resolved.error)
                        if resolved.revoke:
                            return {"ok": False, "error": "project_person_my_selection_empty", "status": 409}
                        row_config[target] = await host._catalog_config(active, owner_subject=target)
                        base = _with_selection(
                            base, resolved=resolved, catalog_version=catalog_version,
                            resource_row_for=lambda resource, owner=target: host._configured_resource(
                                resource, config=row_config[owner]
                            ),
                        )
                else:
                    raise CardLifecyclePlanRefused("card_plan_kind_invalid", 400)
                key = (subject_hash_for(base.grantor_subject), base.access_id)
                if key in {(member["subject_hash"], member["access_id"]) for member in members}:
                    raise CardLifecyclePlanRefused("card_group_member_duplicate")
                existing = await cards.load_current(base.access_id, subject_hash=key[0])
                if existing is not None:
                    raise CardLifecyclePlanRefused("card_plan_target_exists")
                planned[ref] = base
                members.append(group_member(original=None, candidate=base, action="create"))
                originals[(subject_hash_for(base.grantor_subject), base.access_id)] = None
                del pending[ref]
                progressed = True
            if not progressed:
                raise CardLifecyclePlanRefused("card_plan_parent_cycle")

        removals: dict[str, dict[str, CardAuthority]] = {}
        for index, raw in enumerate(updates):
            if (not isinstance(raw, Mapping)
                    or not {"kind", "target_subject", "access_id", "subject_hash", "original_revision"} <= set(raw)
                    or set(raw) - {"kind", "target_subject", "access_id", "subject_hash", "original_revision", "parent",
                                   "selection"}
                    or ("selection" in raw) != (raw.get("kind") == "reselect")):
                raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
            decision = authorization.decision_for(f"update:{index}")
            access_id = _required_text(raw["access_id"], "card_plan_update_invalid")
            subject_hash = _required_text(raw["subject_hash"], "card_plan_update_invalid")
            original_revision = raw["original_revision"]
            if type(original_revision) is not int or original_revision < 1:
                raise CardLifecyclePlanRefused("card_plan_revision_invalid", 400)
            loaded = await cards.load_current(access_id, subject_hash=subject_hash)
            if loaded is None:
                raise CardLifecyclePlanRefused("card_plan_update_target_absent")
            original = loaded[0]
            if (subject_hash_for(original.grantor_subject) != subject_hash
                    or original.card_revision != original_revision):
                raise CardLifecyclePlanRefused("card_plan_original_revision_changed")
            try:
                person_identity = ProjectPersonControlIdentity.from_authority(original)
            except ProjectPersonControlError:
                person_identity = None
            try:
                invitation_identity = ProjectInvitationControlIdentity.from_authority(original)
            except ProjectInvitationControlError:
                invitation_identity = None
            target = _required_text(raw["target_subject"], "card_plan_target_invalid")
            if raw["kind"] == "reselect":
                # W607: an existing person Control or My Card takes a PB-supplied selection. The helper
                # checks its identity, project, person and the step's decision; nothing is written.
                if "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                from connection_hub.delegated_credentials.existing_card_selection_plan import (
                    build_existing_card_selection_update,
                )
                built = await build_existing_card_selection_update(
                    host, original=original, selection=raw["selection"], active=active, decision=decision,
                    project_ref=scope, target_subject=target, actor_subject=actor, request_id=request_id, now=now)
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if raw["kind"] == "remove_person":
                if "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                card_kind, candidate = removed_person_card(
                    original, project_ref=scope, target_subject=target, decision=decision,
                    actor_subject=actor, request_id=request_id, now=now)
                person = removals.setdefault(target, {})
                if card_kind in person:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                person[card_kind] = original
                members.append(group_member(original=original, candidate=candidate, action="revoke"))
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if (person_identity is None and invitation_identity is None
                    or person_identity is not None and person_identity.project_ref != scope
                    or invitation_identity is not None and invitation_identity.project_ref != scope
                    or person_identity is not None
                    and person_identity.target_subject != target
                    or invitation_identity is not None
                    and invitation_identity.invitation_ref != target
                    or decision.target_subject != target):
                raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403)
            action = raw["kind"]
            if action == "revoke":
                if invitation_identity is None or "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                if original.state != CARD_STATE_ACTIVE:
                    raise CardLifecyclePlanRefused("card_plan_revoke_not_active")
                candidate = replace_state(original, CARD_STATE_REVOKED)
            elif action == "attach":
                if person_identity is None or "parent" not in raw:
                    raise CardLifecyclePlanRefused("card_plan_attach_identity_invalid", 400)
                if original.state != CARD_STATE_ACTIVE or original.control_card is not None:
                    raise CardLifecyclePlanRefused("card_plan_attach_conflict")
                parent = await parent_for(raw.get("parent"))
                if parent is None:
                    raise CardLifecyclePlanRefused("card_plan_parent_invalid", 400)
                parent_raw = raw["parent"]
                _require_project_parent(
                    parent, project_ref=scope, authorization=decision,
                    planned_parent=isinstance(parent_raw, Mapping) and set(parent_raw) == {"ref"},
                )
                candidate = dataclasses.replace(
                    original, card_revision=original.card_revision + 1,
                    control_card=_parent_binding(parent),
                )
            else:
                raise CardLifecyclePlanRefused("card_plan_update_action_invalid", 400)
            members.append(group_member(original=original, candidate=candidate, action=action))
            originals[(subject_hash, access_id)] = original.to_dict()

        # A person leaves whole: an active Control and My end in this one
        # decision, never one now and the other by a later write.
        for target, removed in removals.items():
            for card_kind, card_id in (("control", ProjectPersonControlIdentity.build(
                    project_ref=scope, target_subject=target).control_id),
                    ("my", ProjectPersonCardIdentity.build(project_ref=scope, person_subject=target).my_card_id)):
                if card_kind in removed:
                    continue
                grantor = (ProjectPersonControlIdentity.build(project_ref=scope, target_subject=target).project_subject
                           if card_kind == "control" else target)
                # Held as a read: the partner may not come back between plan and decision.
                partner = await load_live(subject_hash_for(grantor), card_id)
                if partner is not None and partner.state == CARD_STATE_ACTIVE:
                    raise CardLifecyclePlanRefused("card_plan_remove_person_incomplete")
            # A revoked member composes no chain, but the decision still holds
            # the Control it was bound under: a group member, or read present.
            member_keys = {(member["subject_hash"], member["access_id"]) for member in members}
            for card in removed.values():
                binding = card.control_card
                if binding is None:
                    continue
                parent = (subject_hash_for(str(binding.holder_subject or "") or card.grantor_subject),
                          binding.control_id)
                if parent not in member_keys and await load_live(*parent) is None:
                    raise CardLifecyclePlanRefused("card_group_control_read_missing")

        # The graph helper resolves planned parents before current live
        # parents and invokes the same hierarchy composer staging uses.
        from connection_hub.delegated_credentials.cards.card_group import compose_group_chains

        await compose_group_chains(members, load_live)
        reads = [reads_by_key[key] for key in sorted(reads_by_key)]
        candidate_value = group_candidate_value(members)
        # W607: the public before/after of every member, from the exact originals loaded above.
        from connection_hub.delegated_credentials.plan_display import plan_display
        display = plan_display(candidate_value, originals)
        participant_input = hub_group_participant_input(
            members=members, actor_subject=actor, actor_kind=actor_kind,
            reads=reads, catalog_version_digest=digest,
        )
        return {"ok": True, "plan": {
            "candidate_value": candidate_value,
            "participant_input": participant_input,
            "reads": reads,
            "catalog_digest": digest,
            "display": display,
        }}
    except CardLifecyclePlanRefused as exc:
        return {"ok": False, "error": exc.reason, "status": exc.status}
    except ProjectAuthorizationError:
        return {"ok": False, "error": "card_plan_authorization_invalid", "status": 403}
    except DecisionRefused as exc:
        return {"ok": False, "error": getattr(exc, "reason", str(exc)), "status": 409}
    except CatalogUnavailable as exc:
        return {"ok": False, "error": "delegated_catalog_unavailable",
                "reason": exc.reason, "retryable": True, "status": 503}
    except CardUnavailable as exc:
        return {"ok": False, "error": "delegated_cards_unavailable",
                "reason": exc.reason, "retryable": True, "status": 503}
    except (ProjectPersonControlError, ProjectInvitationControlError, ProjectIdentityLifecycleError) as exc:
        return {"ok": False, "error": exc.reason, "status": 400}


__all__ = ["build_application_control", "build_project_person_control", "plan_card_lifecycle"]

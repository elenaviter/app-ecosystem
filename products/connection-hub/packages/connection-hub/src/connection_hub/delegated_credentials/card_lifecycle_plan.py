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
    project_authority_subject,
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
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    PROJECT_INVITATION_CONTROL_PROPERTY,
    ProjectInvitationControlAudit,
    bind_project_invitation_control,
    ProjectInvitationControlError,
    ProjectInvitationControlIdentity,
)
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.project_invitation_claim import PROJECT_INVITATION_BINDING_PROVENANCE


def invitation_seeded_person_control(
    pending: CardAuthority, *, invitation_ref: str, project_ref: str, target_subject: str,
    decision: ProjectAuthorizationDecision, actor_subject: str, request_id: str, parent: CardAuthority,
    now: int,
) -> tuple[CardAuthority, dict[str, Any]]:
    """W502: the person's C from one ACTIVE pending invitation Card, as the direct bind builds it.

    The pending Card's own selection, label and link carry over (an admin's edit
    after inviting included). The host's decision for this step must carry the
    verified-session email digest, and it must equal the Card's target digest.
    Returns the C (bound under P) and the claim marker it and My carry.
    """
    from connection_hub.delegated_credentials.cards.model import CONTROL_COMPOSITION_AND
    from connection_hub.delegated_credentials.project_invitation_binding import PROJECT_INVITATION_BINDING_SCHEMA

    try:
        invitation = ProjectInvitationControlIdentity.from_authority(pending)
    except ProjectInvitationControlError:
        raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403) from None
    if (invitation.project_ref != project_ref or invitation.invitation_ref != invitation_ref
            or invitation.control_id != pending.access_id):
        raise CardLifecyclePlanRefused("card_plan_update_scope_invalid", 403)
    if pending.state != CARD_STATE_ACTIVE:
        raise CardLifecyclePlanRefused("project_invitation_control_not_active")
    evidence = dict(decision.evidence or {})
    if (evidence.get("invitation_ref") != invitation_ref
            or evidence.get("invitation_email_digest") != invitation.target_email_digest):
        raise CardLifecyclePlanRefused("project_invitation_binding_email_mismatch", 403)
    person = ProjectPersonControlIdentity.build(project_ref=project_ref, target_subject=target_subject)
    public_properties = copy.deepcopy(dict(pending.properties or {}))
    public_properties.pop(PROJECT_INVITATION_CONTROL_PROPERTY, None)
    live = bind_project_person_control(
        new_credentialless_card(
            control_id=person.control_id, grantor_subject=person.project_subject,
            catalog_version=pending.catalog_version, initial_selection=pending,
            issuer_ref=project_ref, issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
            issuer_label=pending.issuer_label or pending.label, manage_url=pending.manage_url,
            properties=public_properties, composition_mode=CONTROL_COMPOSITION_AND, now=now,
        ),
        identity=person,
    )
    marker = {"schema": PROJECT_INVITATION_BINDING_SCHEMA, "project_ref": project_ref,
              "invitation_ref": invitation_ref, "pending_control_id": pending.access_id,
              "control_id": person.control_id, "person_subject": target_subject,
              "target_email_digest": invitation.target_email_digest, "request_id": request_id, "bound_at": now}
    audit = ProjectPersonControlAudit.build(
        action="bound_from_invitation", actor_subject=actor_subject, identity=person,
        request_id=request_id, occurred_at=now, before=None, after=live)
    provenance = copy.deepcopy(dict(live.provenance or {}))
    provenance[PROJECT_INVITATION_BINDING_PROVENANCE] = marker
    live = bind_project_person_control(dataclasses.replace(live, provenance=provenance), identity=person, audit=audit)
    return dataclasses.replace(live, control_card=_parent_binding(parent)), marker


def fresh_card_over_existing(fresh: CardAuthority, existing: CardAuthority, *, actor_subject: str,
                             request_id: str, now: int) -> CardAuthority:
    """W502/W661: the freshly built ``fresh`` Card, placed at ``existing``'s next revision.

    Operator, 10 Oct: an invitation "IS new card. even if something existed fine. we simply now
    UPSERT. overwrite". Whatever is on the stable id (revoked or still active) is overwritten.
    Only the revision moves onto the existing Card's chain, so (access_id, revision) never
    repeats; every other field is the fresh Card's own. A person Control's "created" audit is
    rebuilt for that revision.
    """
    if fresh.access_id != existing.access_id or fresh.grantor_subject != existing.grantor_subject:
        raise CardLifecyclePlanRefused("card_plan_target_exists")
    candidate = dataclasses.replace(fresh, card_revision=existing.card_revision + 1)
    if candidate.issuer_kind == PROJECT_PERSON_CONTROL_ISSUER_KIND:
        identity = ProjectPersonControlIdentity.from_authority(candidate)
        from connection_hub.delegated_credentials.controls.project_person import (
            PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
        )
        fresh_audit = dict(fresh.provenance or {}).get(PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE) or {}
        audit = ProjectPersonControlAudit.build(
            action=str(fresh_audit.get("action") or "created"), actor_subject=actor_subject, identity=identity,
            request_id=request_id, occurred_at=now, before=None, after=candidate)
        candidate = bind_project_person_control(candidate, identity=identity, audit=audit)
    return candidate


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


def _protected_grants_valid(value: Any) -> bool:
    """W661 S5 (EMain 18:22Z): a fixed PB policy, never Card data: at most 4 resources x 16 grants."""
    # Entry types first: a dict or list entry is valid JSON but unhashable (Infra 18:36Z).
    return (isinstance(value, Mapping) and 0 < len(value) <= 4 and all(
        type(resource) is str and resource and type(grants) is list and 0 < len(grants) <= 16
        and all(type(grant) is str and grant and grant == grant.strip() for grant in grants)
        and len(set(grants)) == len(grants)
        for resource, grants in value.items()))


def _require_protected_grants_kept(protected: Mapping[str, Any] | None, original: CardAuthority,
                                   candidate: Mapping[str, Any]) -> None:
    """A manual edit leaves each listed grant exactly as the original Card holds it (added or removed refuses).

    The Hub only compares the grants it is given; it learns nothing about roles. A role change takes
    role_selection, the one path that may change them.
    """
    for resource, grants in (protected or {}).items():
        listed = set(grants)
        before = listed & set((original.resource_grants or {}).get(resource, ()))
        after = listed & set((candidate.get("resource_grants") or {}).get(resource, ()))
        if before != after:
            raise CardLifecyclePlanRefused("card_edit_admin_grant_role_only")


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
        invitation_seeds: dict[str, tuple[int, Mapping[str, Any]]] = {}
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
                "project_person_control": {"target_subject", "label", "manage_url", "composition_mode", "seed_origin",
                                           "invitation"},
                "project_person_my_card": {"person_subject", "label", "manage_url"},
                "project_invitation_control": {"invitation_ref", "target_email", "label", "manage_url"},
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
                elif kind == "project_person_control" and identity.get("seed_origin") == "invitation":
                    # W502: an invitation's redemption, through the one decision. The person's C is
                    # built from the pending invitation Card exactly as the direct bind builds it.
                    if parent is None:
                        raise CardLifecyclePlanRefused("card_plan_project_control_missing")
                    target = _required_text(identity.get("target_subject"), "card_plan_target_invalid")
                    if target != decision.target_subject:
                        raise CardLifecyclePlanRefused("card_plan_authorization_target_mismatch", 403)
                    if set(identity) != {"target_subject", "seed_origin", "invitation"} or selection:
                        raise CardLifecyclePlanRefused("card_plan_invitation_seed_invalid", 400)
                    _require_project_parent(
                        parent, project_ref=scope, authorization=decision,
                        planned_parent=isinstance(parent_raw, Mapping) and set(parent_raw) == {"ref"},
                    )
                    named = identity.get("invitation")
                    if (not isinstance(named, Mapping)
                            or set(named) != {"invitation_ref", "control_id", "original_revision"}
                            or type(named["original_revision"]) is not int or named["original_revision"] < 1):
                        raise CardLifecyclePlanRefused("card_plan_invitation_seed_invalid", 400)
                    holder = ProjectPersonControlIdentity.build(project_ref=scope, target_subject=target).project_subject
                    loaded = await cards.load_current(
                        _required_text(named["control_id"], "card_plan_invitation_seed_invalid"),
                        subject_hash=subject_hash_for(holder))
                    if loaded is None or loaded[0].card_revision != named["original_revision"]:
                        raise CardLifecyclePlanRefused("card_plan_original_revision_changed")
                    base, marker = invitation_seeded_person_control(
                        loaded[0],
                        invitation_ref=_required_text(named["invitation_ref"], "card_plan_invitation_seed_invalid"),
                        project_ref=scope, target_subject=target, decision=decision, actor_subject=actor,
                        request_id=request_id, parent=parent, now=now)
                    invitation_seeds[loaded[0].access_id] = (loaded[0].card_revision, marker)
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
                    # The direct bind gives My the same claim marker as its invitation-built C.
                    claim = dict(parent.provenance or {}).get(PROJECT_INVITATION_BINDING_PROVENANCE)
                    base = new_project_person_my_card(
                        control_card=parent, label=str(identity.get("label") or ""),
                        manage_url=str(identity.get("manage_url") or ""), now=now,
                        initial_provenance=(None if claim is None
                                            else {PROJECT_INVITATION_BINDING_PROVENANCE: copy.deepcopy(claim)}),
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
                elif kind == "project_invitation_control":
                    # W661 P4: the pending invitation Card, built exactly as the direct create builds it
                    # (project_invitation_pending): credentialless, AND, bound to the invitation identity, the
                    # PB-chosen selection resolved under PB's delegable grants, snapshot "created", audited.
                    if parent is not None:
                        raise CardLifecyclePlanRefused("card_plan_invitation_identity_invalid", 400)
                    try:
                        invitation = ProjectInvitationControlIdentity.build(
                            project_ref=scope,
                            invitation_ref=_required_text(identity.get("invitation_ref"),
                                                          "card_plan_invitation_identity_invalid"),
                            target_email=_required_text(identity.get("target_email"),
                                                        "card_plan_invitation_identity_invalid"))
                    except (ProjectInvitationControlError, ValueError) as exc:
                        raise CardLifecyclePlanRefused("card_plan_invitation_identity_invalid", 400) from exc
                    if invitation.invitation_ref != decision.target_subject:
                        raise CardLifecyclePlanRefused("card_plan_authorization_target_mismatch", 403)
                    base = bind_project_invitation_control(new_credentialless_card(
                        control_id=invitation.control_id, grantor_subject=invitation.project_subject,
                        catalog_version=catalog_version, issuer_ref=invitation.invitation_ref,
                        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND,
                        issuer_label=str(identity.get("label") or "") or "Pending project invitation",
                        manage_url=str(identity.get("manage_url") or ""), properties=selection.get("properties"),
                        composition_mode="and", now=now), identity=invitation)
                    if selected:
                        from connection_hub.delegated_credentials.automation_access import record_from_card
                        resolved = await host._resolve_card_authority(
                            user={"user_id": project_authority_subject(scope), "roles": [], "permissions": []},
                            existing=record_from_card(base), active=active,
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
                            return {"ok": False, "error": "project_invitation_control_selection_empty",
                                    "status": 409}
                        row_config[invitation.project_subject] = await host._catalog_config(
                            active, owner_subject=invitation.project_subject)
                        base = _with_selection(
                            base, resolved=resolved, catalog_version=catalog_version,
                            resource_row_for=lambda resource, owner=invitation.project_subject:
                                host._configured_resource(resource, config=row_config[owner]))
                    created = bind_project_invitation_control(
                        materialize_control_snapshot(base, basis_catalog_version=catalog_version, origin="created"),
                        identity=invitation)
                    base = bind_project_invitation_control(created, identity=invitation,
                        audit=ProjectInvitationControlAudit.build(
                            action="created", actor_subject=actor, identity=invitation, request_id=request_id,
                            occurred_at=now, before=None, after=created))
                else:
                    raise CardLifecyclePlanRefused("card_plan_kind_invalid", 400)
                key = (subject_hash_for(base.grantor_subject), base.access_id)
                if key in {(member["subject_hash"], member["access_id"]) for member in members}:
                    raise CardLifecyclePlanRefused("card_group_member_duplicate")
                existing = await cards.load_current(base.access_id, subject_hash=key[0])
                original = None
                if existing is not None:
                    # W502 (operator, 7 Oct: a removed person is simply "newly invited" and
                    # gets fresh Cards) and W661 (operator, 10 Oct: "UPSERT. overwrite"): a
                    # person's C or My on its stable id is created again, freshly built, at
                    # its next revision, whether revoked or still active. Nothing of the old
                    # Card is carried over. A project Control is never overwritten this way.
                    original = existing[0]
                    if raw["kind"] not in ("project_person_control", "project_person_my_card"):
                        raise CardLifecyclePlanRefused("card_plan_target_exists")
                    base = fresh_card_over_existing(base, original, actor_subject=actor, request_id=request_id,
                                                    now=now)
                planned[ref] = base
                members.append(group_member(original=original, candidate=base,
                                            action="create" if original is None else "recreate"))
                originals[(subject_hash_for(base.grantor_subject), base.access_id)] = (
                    None if original is None else original.to_dict())
                del pending[ref]
                progressed = True
            if not progressed:
                raise CardLifecyclePlanRefused("card_plan_parent_cycle")

        removals: dict[str, dict[str, CardAuthority]] = {}
        already_applied = 0
        for index, raw in enumerate(updates):
            if (not isinstance(raw, Mapping)
                    or not {"kind", "target_subject", "access_id", "subject_hash", "original_revision"} <= set(raw)
                    or set(raw) - {"kind", "target_subject", "access_id", "subject_hash", "original_revision", "parent",
                                   "selection", "profile", "resource", "display_digest", "control",
                                   "add_grants", "add_operations", "remove_operations", "operation_grants",
                                   "protected_grants"}
                    or ("selection" in raw) != (raw.get("kind") in {"reselect", "reselect_project_control",
                                                                    "reselect_agent_card",
                                                                    "reselect_invitation_control"})):
                raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
            decision = authorization.decision_for(f"update:{index}")
            access_id = _required_text(raw["access_id"], "card_plan_update_invalid")
            subject_hash = _required_text(raw["subject_hash"], "card_plan_update_invalid")
            original_revision = raw["original_revision"]
            # W639: the project host knows an agent Card's id, not its revision.
            # For the agent lifecycle kinds, 0 plans against the revision the Hub
            # loads now; the member carries it and PREPARE fences exactly that.
            # W661: a role_selection likewise applies PB's policy delta to the Card's current version.
            # W661 P4: a person's removal likewise ends the C and My the Hub reads now (PB reads no Card).
            read_current = (raw.get("kind") in {"attach_agent", "detach_agent", "apply_agent_profile",
                                                "role_selection", "remove_person"}
                            and original_revision == 0)
            if type(original_revision) is not int or (original_revision < 1 and not read_current):
                raise CardLifecyclePlanRefused("card_plan_revision_invalid", 400)
            loaded = await cards.load_current(access_id, subject_hash=subject_hash)
            if loaded is None:
                raise CardLifecyclePlanRefused("card_plan_update_target_absent")
            original = loaded[0]
            if (subject_hash_for(original.grantor_subject) != subject_hash
                    or not read_current and original.card_revision != original_revision):
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
            if "protected_grants" in raw and (raw["kind"] != "reselect"
                                              or not _protected_grants_valid(raw["protected_grants"])):
                raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
            if raw["kind"] == "reselect":
                # W607: an existing person Control or My Card takes a PB-supplied selection. The helper
                # checks its identity, project, person and the step's decision; nothing is written.
                if "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                from connection_hub.delegated_credentials.existing_card_selection_plan import (
                    build_existing_card_selection_update,
                )
                # S5 first on what the person submitted, so a protected-grant change is refused by its own
                # name before catalog resolution can refuse it for another reason; then on the candidate.
                submitted = raw["selection"].get("resource_grants")
                if isinstance(submitted, Mapping):
                    # A supplied dimension replaces the whole map (W607), so compare the map as submitted.
                    _require_protected_grants_kept(raw.get("protected_grants"), original,
                                                   {"resource_grants": dict(submitted)})
                built = await build_existing_card_selection_update(
                    host, original=original, selection=raw["selection"], active=active, decision=decision,
                    project_ref=scope, target_subject=target, actor_subject=actor, request_id=request_id, now=now)
                _require_protected_grants_kept(raw.get("protected_grants"), original, built["member"]["candidate"])
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if raw["kind"] in {"reselect_project_control", "reselect_agent_card", "reselect_invitation_control"}:
                # W638: the project's own Control or a project agent Card takes a host-authorized selection.
                if "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                from connection_hub.delegated_credentials.managed_card_selection_plan import (
                    build_managed_card_selection_update,
                )
                built = await build_managed_card_selection_update(
                    host, original=original, selection=raw["selection"], active=active, decision=decision,
                    kind={"reselect_project_control": "project_control", "reselect_agent_card": "agent_card",
                          "reselect_invitation_control": "invitation_control"}[raw["kind"]],
                    project_ref=scope, actor_subject=actor, request_id=request_id, now=now)
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if raw["kind"] == "role_selection":
                # W661 (EMain 18:16Z): a role change's Card side. PB sends a fixed policy delta, never Card
                # content; the Hub applies it to the Card it just read and builds through the reselect builder.
                from connection_hub.delegated_credentials.existing_card_selection_plan import (
                    build_existing_card_selection_update,
                )
                from connection_hub.delegated_credentials.role_selection_plan import (
                    role_selection, role_update_shape_valid,
                )
                if not role_update_shape_valid(raw):
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                selection = role_selection(original, raw)
                if selection is None:
                    already_applied += 1  # unchanged: the Card stays a read, never a synthetic revision
                    continue
                built = await build_existing_card_selection_update(
                    host, original=original, selection=selection, active=active, decision=decision,
                    project_ref=scope, target_subject=target, actor_subject=actor, request_id=request_id, now=now)
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if raw["kind"] == "reset_to_control":
                # W661 (EMain 18:07Z): My Reset as a STAGE-owned step; the Hub reads My and its Control and
                # recomputes the display the person saw. Only the shape PB may send is accepted.
                from connection_hub.delegated_credentials.managed_card_reset_plan import (
                    build_reset_to_control_update, reset_update_shape_valid,
                )
                if not reset_update_shape_valid(raw):
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                built = await build_reset_to_control_update(
                    host, original=original, update=raw, decision=decision, project_ref=scope,
                    actor_subject=actor, request_id=request_id)
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                reads_by_key[(built["read"]["subject_hash"], built["read"]["access_id"])] = built["read"]
                continue
            if ("display_digest" in raw or "control" in raw
                    or ("profile" in raw or "resource" in raw) and raw["kind"] != "apply_agent_profile"):
                raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
            if raw["kind"] in {"attach_agent", "detach_agent", "apply_agent_profile"}:
                if ("parent" in raw) != (raw["kind"] == "attach_agent"):
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                from connection_hub.delegated_credentials.agent_lifecycle_plan import (
                    build_agent_lifecycle_update,
                )
                parent = await parent_for(raw["parent"]) if raw["kind"] == "attach_agent" else None
                built = await build_agent_lifecycle_update(
                    host, original=original,
                    update={**raw, "original_revision": original.card_revision} if read_current else raw, active=active, decision=decision,
                    project_ref=scope, actor_subject=actor, request_id=request_id, now=now,
                    parent=parent)
                members.append(built["member"])
                originals[(subject_hash, access_id)] = original.to_dict()
                continue
            if raw["kind"] == "remove_person":
                if "parent" in raw:
                    raise CardLifecyclePlanRefused("card_plan_update_invalid", 400)
                if read_current and original.state == CARD_STATE_REVOKED:
                    # W661 v6.3 item 6: the same-request retry of a removal whose PUBLISH already happened. The
                    # Card is exactly this person's (checked as if active), already revoked: nothing to write.
                    removed_person_card(replace_state(original, CARD_STATE_ACTIVE), project_ref=scope,
                                        target_subject=target, decision=decision, actor_subject=actor,
                                        request_id=request_id, now=now)
                    already_applied += 1
                    continue
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

        if not members:
            # W661 v6.3 item 6: every update already holds (a role already applied, a person already removed).
            # The card_version handler answers a signed already_applied; nothing is written.
            raise CardLifecyclePlanRefused(
                "card_plan_already_applied" if already_applied == len(updates) and not creations
                else "card_plan_role_unchanged")
        # An invitation-built C consumes its pending Card in this same decision.
        revoked_here = {(member["access_id"], member["original_revision"]) for member in members
                        if member["action"] == "revoke"}
        for pending_id, (revision, _marker) in invitation_seeds.items():
            if (pending_id, revision) not in revoked_here:
                raise CardLifecyclePlanRefused("card_plan_invitation_not_consumed")

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

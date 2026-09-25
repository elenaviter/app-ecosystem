# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Claim-first conversion of a pending invitation Card into person authority."""

from __future__ import annotations

import copy
import dataclasses
import time
from typing import Any

from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CONTROL_COMPOSITION_AND,
    CardRecordError,
)
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
    CardServingUnavailable,
    replace_state,
)
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_PROPERTY,
    ProjectInvitationControlAudit,
    ProjectInvitationControlError,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlAudit,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
    bind_project_person_control,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    ProjectIdentityLifecycle,
    ProjectIdentityLifecycleError,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingError,
    ProjectInvitationBindingResolver,
)
from connection_hub.delegated_credentials.project_invitation_claim import (
    PROJECT_INVITATION_BINDING_PROVENANCE,
    ProjectInvitationClaimPolicy,
)
from connection_hub.delegated_credentials.project_invitation_pending import (
    AuthorityFromRecord,
    ProjectInvitationPendingCards,
    RecordFromAuthority,
)


def _serving_state_unavailable(exc: CardServingUnavailable) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "delegated_card_serving_state_unavailable",
        "reason": exc.reason,
        "access_id": exc.access_id,
        "retryable": True,
        "status": 503,
    }


class ProjectInvitationRedemption:
    """Own the durable claim and every repairable person-side write."""

    def __init__(
        self,
        *,
        host: Any,
        pending_cards: ProjectInvitationPendingCards,
        binding_resolver: ProjectInvitationBindingResolver | None,
        authority_from_record: AuthorityFromRecord,
        record_from_authority: RecordFromAuthority,
    ) -> None:
        self._host = host
        self._pending_cards = pending_cards
        self._claims = ProjectInvitationClaimPolicy(
            binding_resolver=binding_resolver,
        )
        self._authority_from_record = authority_from_record
        self._record_from_authority = record_from_authority
        self._project_identities = ProjectIdentityLifecycle(
            host=host,
            authority_from_record=authority_from_record,
            record_from_authority=record_from_authority,
        )

    async def _load_live(
        self,
        identity: ProjectPersonControlIdentity,
    ) -> tuple[Any, str, Any] | dict[str, Any]:
        try:
            loaded = await self._host._load_record_any_state(
                identity.control_id,
                grantor_subject=identity.project_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "project_person_control_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if loaded is None:
            return None, "", None
        record, state = loaded
        try:
            stored_identity = ProjectPersonControlIdentity.from_authority(
                self._authority_from_record(record)
            )
        except ProjectPersonControlError as exc:
            return {
                "ok": False,
                "error": "project_person_control_identity_conflict",
                "reason": exc.reason,
                "status": 409,
            }
        if stored_identity != identity or state != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "project_invitation_binding_live_control_conflict",
                "status": 409,
            }
        marker = dict(self._authority_from_record(record).provenance or {}).get(
            PROJECT_INVITATION_BINDING_PROVENANCE
        )
        if marker is None:
            return {
                "ok": False,
                "error": "project_invitation_binding_live_control_conflict",
                "status": 409,
            }
        return record, state, marker

    async def _claim_pending(
        self,
        *,
        pending_record: Any,
        pending: Any,
        pending_identity: Any,
        marker: dict[str, Any],
        actor_subject: str,
    ) -> None:
        """Consume the invitation under its Card revision before person writes."""

        revoked = bind_project_invitation_control(
            replace_state(pending, CARD_STATE_REVOKED),
            identity=pending_identity,
        )
        provenance = copy.deepcopy(dict(revoked.provenance or {}))
        provenance[PROJECT_INVITATION_BINDING_PROVENANCE] = marker
        revoked = bind_project_invitation_control(
            dataclasses.replace(revoked, provenance=provenance),
            identity=pending_identity,
        )
        audit = ProjectInvitationControlAudit.build(
            action="bound",
            actor_subject=actor_subject,
            identity=pending_identity,
            request_id=marker["request_id"],
            occurred_at=marker["bound_at"],
            before=pending,
            after=revoked,
        )
        revoked_record = self._record_from_authority(
            bind_project_invitation_control(
                revoked,
                identity=pending_identity,
                audit=audit,
            )
        )
        await self._host._forget_record(
            pending_record,
            revoked_record=revoked_record,
        )

    async def bind(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Consume one pending Card before creating any person-side authority."""

        actor = str(actor_subject or "").strip()
        if not actor:
            return {
                "ok": False,
                "error": "project_invitation_binding_person_subject_missing",
                "status": 400,
            }
        evidence = await self._claims.evidence(
            actor_subject=actor,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(evidence, dict):
            return evidence
        loaded = await self._pending_cards.load(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )
        if isinstance(loaded, dict):
            return loaded
        pending_record, pending_state, pending_identity = loaded
        if pending_identity.target_email_digest != evidence.email_digest:
            return {
                "ok": False,
                "error": "project_invitation_binding_email_mismatch",
                "status": 403,
            }
        pending = dataclasses.replace(
            self._authority_from_record(pending_record),
            state=pending_state,
        )
        pending_marker = dict(pending.provenance or {}).get(
            PROJECT_INVITATION_BINDING_PROVENANCE
        )
        if pending_state == CARD_STATE_REVOKED and pending_marker is None:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }
        if pending_state not in {CARD_STATE_ACTIVE, CARD_STATE_REVOKED}:
            return {
                "ok": False,
                "error": "project_invitation_control_not_active",
                "status": 409,
            }

        live_identity = ProjectPersonControlIdentity.build(
            project_ref=project_ref,
            target_subject=actor,
        )
        loaded_live = await self._load_live(live_identity)
        if isinstance(loaded_live, dict):
            return loaded_live
        live_record, _live_state, live_marker = loaded_live
        marker_source = pending_marker if pending_marker is not None else live_marker
        try:
            marker = (
                self._claims.validate_marker(
                    marker_source,
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                )
                if marker_source is not None
                else self._claims.new_marker(
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                    request_id=str(request_id or "").strip(),
                    bound_at=int(time.time()),
                )
            )
            if live_marker is not None:
                self._claims.validate_marker(
                    live_marker,
                    evidence=evidence,
                    target_email_digest=pending_identity.target_email_digest,
                    live_control_id=live_identity.control_id,
                )
        except ProjectInvitationBindingError as exc:
            return {"ok": False, "error": exc.reason, "status": 409}
        if not marker.get("request_id"):
            return {
                "ok": False,
                "error": "project_invitation_binding_request_id_missing",
                "status": 400,
            }

        try:
            if pending_state == CARD_STATE_ACTIVE:
                await self._claim_pending(
                    pending_record=pending_record,
                    pending=pending,
                    pending_identity=pending_identity,
                    marker=marker,
                    actor_subject=actor,
                )
        except CardConflict as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_claim_conflict",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 409,
            }
        except ProjectInvitationControlError as exc:
            return {"ok": False, "error": exc.reason, "status": 409}
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }

        created = False
        try:
            if live_record is None:
                public_properties = copy.deepcopy(dict(pending.properties or {}))
                public_properties.pop(PROJECT_INVITATION_CONTROL_PROPERTY, None)
                live = bind_project_person_control(
                    new_credentialless_card(
                        control_id=live_identity.control_id,
                        grantor_subject=live_identity.project_subject,
                        catalog_version=pending.catalog_version,
                        initial_selection=pending,
                        issuer_ref=live_identity.project_ref,
                        issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
                        issuer_label=pending.issuer_label or pending.label,
                        manage_url=pending.manage_url,
                        properties=public_properties,
                        composition_mode=CONTROL_COMPOSITION_AND,
                        now=int(time.time()),
                    ),
                    identity=live_identity,
                )
                audit = ProjectPersonControlAudit.build(
                    action="bound_from_invitation",
                    actor_subject=actor,
                    identity=live_identity,
                    request_id=marker["request_id"],
                    occurred_at=marker["bound_at"],
                    before=None,
                    after=live,
                )
                provenance = copy.deepcopy(dict(live.provenance or {}))
                provenance[PROJECT_INVITATION_BINDING_PROVENANCE] = marker
                live = bind_project_person_control(
                    dataclasses.replace(live, provenance=provenance),
                    identity=live_identity,
                    audit=audit,
                )
                live_record = self._record_from_authority(live)
                await self._host._persist_record(live_record, expected_revision=0)
                created = True
            live = self._authority_from_record(live_record)
            identity_result = await self._project_identities.ensure(
                live_record,
                initial_selection=live,
                initial_provenance={
                    PROJECT_INVITATION_BINDING_PROVENANCE: marker,
                },
            )
        except (
            CardRecordError,
            ControlCardError,
            ProjectIdentityLifecycleError,
            ProjectPersonControlError,
        ) as exc:
            return {
                "ok": False,
                "error": getattr(exc, "reason", str(exc)),
                "status": 409,
            }
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }

        if created:
            await self._host.notify_change(
                actor,
                action="project_invitation_control_bound",
                access=live_record.to_public_dict(),
            )
        return {
            "ok": True,
            "bound": created,
            "pending_control_id": pending_identity.control_id,
            "control_card": live_record.to_public_dict(),
            "my_card": identity_result.my_card.to_public_dict(),
            "project_identity_edge": identity_result.edge.to_dict(),
            "binding": copy.deepcopy(marker),
        }


__all__ = ["ProjectInvitationRedemption"]

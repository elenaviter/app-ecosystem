# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Authoritative live resolution for pointer-backed delegated grants."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from connection_hub.delegated_credentials.cards.cache import (
    CardCacheUnusable,
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_KIND_AGENT,
    CARD_KIND_AUTOMATION,
    CARD_STATE_ACTIVE,
    CardAuthority,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.cards.resolver import (
    CardUnavailable,
    DelegatedCardResolver,
)
from connection_hub.delegated_credentials.controls.project_person_composition import (
    compose_with_project_held_control,
    project_held_control,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.attribution import (
    ResolvedCardComposition,
)
from connection_hub.delegated_credentials.credential_view import (
    resource_matches,
)

ACCESS_SOURCES = ("manual", "oauth", "agent")


class LiveGrantCardError(RuntimeError):
    """The current registry card could not be trusted as authorization state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required_text(value: Any, reason: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LiveGrantCardError(reason)
    return text


async def resolve_live_grant_composition(
    redis: Any,
    *,
    tenant: str,
    project: str,
    access_id: str,
    expected_client_id: str = "",
    expected_grantor_subject: str = "",
    expected_delegate_subject: str = "",
    card_store: Any = None,
) -> ResolvedCardComposition | None:
    """Return current Caller, Control, and Effective authority, or raise.

    A pointer-bearing token has no snapshot fallback. Store failures, malformed
    records, and binding mismatches are authorization failures. A card whose
    Redis projection is missing is restored from its durable revision when a
    card store and the binding's grantor are both available.
    """

    pointer = _required_text(access_id, "access_id_missing")
    cache = DelegatedCardRuntimeCache(redis, tenant=tenant, project=project)
    grantor = str(expected_grantor_subject or "").strip()
    subject_hash = hashlib.sha256(grantor.encode("utf-8")).hexdigest() if grantor else ""

    if card_store is not None and subject_hash:
        # The resolver serves nothing until the current Redis run is swept.
        resolver = DelegatedCardResolver(cache=cache, store=card_store)
        try:
            record = await resolver.resolve(subject_hash=subject_hash, access_id=pointer)
        except CardUnavailable as exc:
            raise LiveGrantCardError(exc.reason) from exc
    else:
        # Without a durable store this caller cannot sweep, so it fails closed
        # until a store owner (the Connection Hub cron) completes the sweep.
        try:
            in_run, entry = await cache.read_in_current_run(pointer)
        except CardCacheUnusable as exc:
            # Without a durable source a damaged projection cannot be repaired,
            # so it is unavailability rather than a revoked card.
            raise LiveGrantCardError(exc.reason) from exc
        except Exception as exc:
            raise LiveGrantCardError("lookup_unavailable") from exc
        if not in_run:
            raise LiveGrantCardError("card_projection_reconciling")
        if entry is None:
            # A missing projection is not a revoked Card: a migration, an
            # eviction or a restored snapshot can remove it while the durable
            # Card stays active. Without a durable store this caller cannot
            # tell, so it answers unavailable, never revoked.
            raise LiveGrantCardError("card_projection_missing")
        if entry.is_updating:
            raise LiveGrantCardError("card_updating")
        if entry.is_revoked:
            return None
        record = entry.authority

    if record is None:
        return None

    if authority_is_credentialless(record):
        raise LiveGrantCardError("caller_card_has_no_credential")
    _required_text(record.client_id, "client_id_missing")
    _required_text(record.grantor_subject, "grantor_subject_missing")
    _required_text(record.delegate_subject, "delegate_subject_missing")
    if record.access_id != pointer:
        raise LiveGrantCardError("access_id_mismatch")
    if record.source not in ACCESS_SOURCES:
        raise LiveGrantCardError("source_invalid")
    if record.state != CARD_STATE_ACTIVE:
        return None
    if record.expires_at <= 0:
        raise LiveGrantCardError("expiry_missing")
    if record.expires_at <= int(time.time()):
        return None

    expected = (
        (expected_client_id, record.client_id, "client_id_mismatch"),
        (expected_grantor_subject, record.grantor_subject, "grantor_subject_mismatch"),
        (expected_delegate_subject, record.delegate_subject, "delegate_subject_mismatch"),
    )
    for expected_value, actual_value, reason in expected:
        clean_expected = str(expected_value or "").strip()
        if clean_expected and clean_expected != actual_value:
            raise LiveGrantCardError(reason)

    caller = record
    control = None
    effective = caller
    if caller.control_card is not None:
        control_id = caller.control_card.control_id
        # A Control Card the project holds for this person is stored under the
        # project's subject, not the caller's (W260, 2026-09-26).
        held = project_held_control(caller)
        # A project Control Card attached through the project path is stored
        # under its creator, named on the binding (W260).
        holder = str(getattr(caller.control_card, "holder_subject", "") or "")
        control_subject_hash = (
            hashlib.sha256(held.grantor_subject.encode("utf-8")).hexdigest()
            if held is not None
            else hashlib.sha256(holder.encode("utf-8")).hexdigest()
            if holder
            else subject_hash
        )
        if card_store is not None and control_subject_hash:
            try:
                control = await DelegatedCardResolver(cache=cache, store=card_store).resolve(
                    subject_hash=control_subject_hash,
                    access_id=control_id,
                )
            except CardUnavailable as exc:
                raise LiveGrantCardError(exc.reason) from exc
        else:
            try:
                in_run, control_entry = await cache.read_in_current_run(control_id)
            except CardCacheUnusable as exc:
                raise LiveGrantCardError(exc.reason) from exc
            except Exception as exc:
                raise LiveGrantCardError("control_card_lookup_unavailable") from exc
            if not in_run:
                raise LiveGrantCardError("card_projection_reconciling")
            if control_entry is None:
                control = None
            elif control_entry.is_updating:
                raise LiveGrantCardError("control_card_updating")
            elif control_entry.is_revoked:
                control = None
            else:
                control = control_entry.authority
        # A legacy project Control Card lives only in Redis, with no durable
        # revision a rollback could be checked against, so it is not
        # authority here. A Card still bound to one fails closed below.
        if control is None:
            raise LiveGrantCardError("control_card_unresolvable")
        if not authority_is_credentialless(control):
            raise LiveGrantCardError("control_card_has_credential")
        try:
            effective = (
                compose_with_project_held_control(caller, control)
                if held is not None
                else effective_card_authority(caller, control)
            )
        except ControlCardMismatch as exc:
            raise LiveGrantCardError(exc.reason) from exc
    return ResolvedCardComposition(
        caller_card=caller,
        control_card=control,
        effective_card=effective,
    )


async def resolve_live_grant_card(
    redis: Any,
    *,
    tenant: str,
    project: str,
    access_id: str,
    expected_client_id: str = "",
    expected_grantor_subject: str = "",
    expected_delegate_subject: str = "",
    card_store: Any = None,
) -> CardAuthority | None:
    """Compatibility view returning only the current Effective Card."""

    composition = await resolve_live_grant_composition(
        redis,
        tenant=tenant,
        project=project,
        access_id=access_id,
        expected_client_id=expected_client_id,
        expected_grantor_subject=expected_grantor_subject,
        expected_delegate_subject=expected_delegate_subject,
        card_store=card_store,
    )
    return composition.effective_card if composition is not None else None


def live_grants_for_resource(
    record: CardAuthority,
    resource: str,
) -> tuple[str, ...] | None:
    """Return live grants for one concrete surface."""

    requested_resource = str(resource or "").strip()
    if not requested_resource:
        return None
    matched = False
    grants: list[str] = []
    for configured_resource, configured_grants in record.resource_grants.items():
        if not resource_matches(str(configured_resource), requested_resource):
            continue
        matched = True
        for grant in configured_grants:
            text = str(grant or "").strip()
            if text and text not in grants:
                grants.append(text)
    return tuple(grants) if matched else None


def whole_card_grants(record: CardAuthority) -> tuple[str, ...] | None:
    """Return the union used only when refreshing a whole-Card credential."""

    if record.card_kind not in {CARD_KIND_AGENT, CARD_KIND_AUTOMATION}:
        return None
    matched = False
    grants: list[str] = []
    for configured_grants in record.resource_grants.values():
        matched = True
        for grant in configured_grants:
            text = str(grant or "").strip()
            if text and text not in grants:
                grants.append(text)
    return tuple(grants) if matched else None


__all__ = [
    "LiveGrantCardError",
    "live_grants_for_resource",
    "resolve_live_grant_card",
    "resolve_live_grant_composition",
    "whole_card_grants",
]

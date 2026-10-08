# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W502: bind a person's project Control C under the project's Control Card P.

The project host names P (``ProjectControlLocator``) in its decision or its
invitation evidence; nothing in a request selects it. The deterministic id of
an application Control is a consistency check on that locator, never a way to
pick another Card. Create, redemption and the repair operation all bind
through ``attach_control_card``, so the caller-writer gate decides it as an
attach (Ops B1), the whole chain is composed before persistence, and the write
is fenced like every other.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, ControlCardBinding
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    control_card_id_for_issuer,
)
from connection_hub.delegated_credentials.controls.project_person import (
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.controls.project_person_composition import (
    PROJECT_CONTROL_ISSUER_KIND,
)
from connection_hub.delegated_credentials.project_authorization import ProjectControlLocator


def _p_invalid(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "outcome": "p_invalid", "status": 409}


def _unavailable(exc: CardUnavailable) -> dict[str, Any]:
    return {"ok": False, "error": "project_control_unavailable", "outcome": "not_bound",
            "reason": getattr(exc, "reason", ""), "retryable": True, "status": 503}


async def check_project_control(
    host: Any, identity: ProjectPersonControlIdentity, locator: ProjectControlLocator | None,
) -> dict[str, Any] | None:
    """A refusal when the named P is not exactly this project's live P; None otherwise."""
    try:
        return await _check(host, identity, locator)
    except CardUnavailable as exc:
        return _unavailable(exc)


async def _check(
    host: Any, identity: ProjectPersonControlIdentity, locator: ProjectControlLocator | None,
) -> dict[str, Any] | None:
    if locator is None:
        return None
    try:
        expected = control_card_id_for_issuer(PROJECT_CONTROL_ISSUER_KIND, identity.project_ref,
                                              grantor_subject=locator.holder_subject)
    except ControlCardError:
        return _p_invalid("project_control_locator_invalid")
    if locator.control_id != expected:
        return _p_invalid("project_control_locator_mismatch")
    loaded = await host._load_record_any_state(locator.control_id, grantor_subject=locator.holder_subject)
    if loaded is None:
        return _p_invalid("project_control_absent")
    record, state = loaded
    if (state != CARD_STATE_ACTIVE or record.issuer_kind != PROJECT_CONTROL_ISSUER_KIND
            or record.issuer_ref != identity.project_ref):
        return _p_invalid("project_control_not_exact")
    if record.control_card is not None:
        # Root option (a): P is the project's top boundary, never under another Card.
        return _p_invalid("project_control_not_root")
    return None


async def bind_project_control(
    host: Any, identity: ProjectPersonControlIdentity, locator: ProjectControlLocator | None,
) -> dict[str, Any]:
    """Bind C under P; the result names this person's outcome.

    ``bound`` and ``already_bound`` succeed (the same P again is a no-op). A
    C bound to another P is moved only when that P is ended or absent (EMain
    R3); a live other P is ``p_conflict``. A P edit never rebinds (R4).
    """
    if locator is None:
        return {"ok": True, "outcome": "no_project_control"}
    try:
        return await _bind(host, identity, locator)
    except CardUnavailable as exc:
        return _unavailable(exc)


async def _bind(
    host: Any, identity: ProjectPersonControlIdentity, locator: ProjectControlLocator,
) -> dict[str, Any]:
    refusal = await _check(host, identity, locator)
    if refusal is not None:
        return refusal
    loaded = await host._load_record_any_state(identity.control_id, grantor_subject=identity.project_subject)
    if loaded is None:
        return {"ok": False, "error": "project_person_control_not_found", "outcome": "control_missing",
                "status": 404}
    record, state = loaded
    if state != CARD_STATE_ACTIVE:
        return {"ok": False, "error": "project_person_control_not_active", "outcome": "control_missing",
                "status": 409}
    replace_control_id = ""
    current = record.control_card
    if current is not None:
        if current.control_id == locator.control_id:
            return {"ok": True, "outcome": "already_bound"}
        old = await host._load_record_any_state(
            current.control_id, grantor_subject=current.holder_subject or identity.project_subject)
        if old is not None and old[1] == CARD_STATE_ACTIVE:
            return {"ok": False, "error": "project_control_conflict", "outcome": "p_conflict", "status": 409,
                    "control_card": current.to_dict()}
        replace_control_id = current.control_id
    result = await host.attach_control_card(
        {"user_id": identity.project_subject}, access_id=identity.control_id, control_id=locator.control_id,
        expected_card_revision=record.card_revision, replace_control_id=replace_control_id,
        _control_holder=locator.holder_subject)
    if result.get("ok") is not True:
        return {**result, "outcome": "not_bound"}
    return {"ok": True, "outcome": "bound" if result.get("attached") else "already_bound"}


async def bound_at_creation(
    host: Any, identity: ProjectPersonControlIdentity, locator: ProjectControlLocator | None, record: Any,
) -> Any:
    """C's first revision, already bound under P, or a refusal; never an unbound C when P is named.

    CodeApp (22:44): a C visible unbound in one write and bound in a later
    one is a business atomicity gap. So create and redemption write C with
    its binding in revision 1: the same binding attach would record, the
    whole chain composed before the write, and the write gated as a
    ``create`` of a bound Card (the binding the candidate takes decides it).
    """
    if locator is None:
        return record
    try:
        refusal = await _check(host, identity, locator)
        if refusal is not None:
            return refusal
        loaded = await host._load_record_any_state(locator.control_id, grantor_subject=locator.holder_subject)
        if loaded is None:
            return _p_invalid("project_control_absent")
        control = loaded[0]
        binding = ControlCardBinding(
            control_id=control.access_id, issuer_ref=control.issuer_ref, issuer_kind=control.issuer_kind,
            issuer_label=control.issuer_label, manage_url=control.manage_url,
            control_revision=control.card_revision, holder_subject=locator.holder_subject)
        bound = dataclasses.replace(record, control_card=binding)
        resolved, _effective = await host._compose_with_control(bound)
    except CardUnavailable as exc:
        return _unavailable(exc)
    except ControlCardMismatch as exc:
        return {"ok": False, "error": "control_card_invalid", "reason": exc.reason, "outcome": "p_invalid",
                "status": 409}
    if resolved is None:
        return {"ok": False, "error": "control_card_invalid", "reason": "control_card_unresolvable",
                "outcome": "p_invalid", "status": 409}
    return bound


__all__ = ["bind_project_control", "bound_at_creation", "check_project_control"]

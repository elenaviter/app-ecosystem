# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661: a person's per-service Reset to Control as a STAGE update (``reset_to_control``).

The Hub knows Cards and PB knows roles (EMain, 18:07Z). PB authorizes the Reset
by role and sends only what the person saw: the My Card, its base version, the
service and the display digest. The Hub reads My and its effective Control,
recomputes the display with the same pure function the Hub showed
(``managed_card_reset_display``), and builds the new My only when the digest
still matches. C is read, never written. Nothing is written here.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .cards.card_group import group_member
from .cards.model import CardAuthority
from .cards.store import subject_hash_for
from .existing_card_selection_plan import _target_identity
from .managed_card_reset import RESET_FIELDS, ManagedCardResetError, managed_card_reset_display
from .project_authorization import PROJECT_PERSON_CONTROL_UPDATE, ProjectAuthorizationDecision

RESET_KEYS = frozenset({"kind", "target_subject", "access_id", "subject_hash", "original_revision", "resource",
                        "display_digest", "control"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def reset_update_shape_valid(raw: Mapping[str, Any]) -> bool:
    control = raw.get("control")
    return (set(raw) == RESET_KEYS and type(raw.get("resource")) is str and bool(raw["resource"])
            and type(raw.get("display_digest")) is str and bool(_DIGEST.fullmatch(raw["display_digest"]))
            and isinstance(control, Mapping) and set(control) == {"access_id", "subject_hash"}
            and all(type(control[key]) is str and control[key] for key in control))


async def build_reset_to_control_update(
    host: Any, *, original: CardAuthority, update: Mapping[str, Any], decision: ProjectAuthorizationDecision,
    project_ref: str, actor_subject: str, request_id: str,
) -> dict[str, Any]:
    """The new My of one Reset, as a group member; refuses by name when what the person saw moved."""
    from .automation_access import card_authority_from_record, record_from_card
    from .caller_writer_gate import CallerWriteRefused
    from .cards.resolver import CardUnavailable

    if (not decision.allowed or decision.operation != PROJECT_PERSON_CONTROL_UPDATE
            or decision.project_ref != project_ref or decision.actor_subject != actor_subject
            or decision.request_id != request_id or decision.target_subject != update["target_subject"]):
        raise DecisionRefused("card_plan_authorization_invalid")
    kind, _identity = _target_identity(original, project_ref, update["target_subject"])
    if kind != "my_card":
        raise DecisionRefused("card_plan_update_scope_invalid")
    binding = original.control_card
    if binding is None or binding.control_id != update["control"]["access_id"]:
        raise DecisionRefused("card_plan_reset_control_moved")
    try:
        control, _ = await host._compose_with_control(record_from_card(original))
    except CardUnavailable as exc:
        raise DecisionRefused("delegated_cards_unavailable") from exc
    except Exception as exc:  # ControlCardMismatch and friends: the Control the person saw is not resolvable
        raise DecisionRefused("card_plan_reset_control_moved") from exc
    if control is None:
        raise DecisionRefused("card_plan_reset_control_moved")
    read = {"subject_hash": subject_hash_for(control.grantor_subject), "access_id": control.access_id,
            "revision": control.card_revision}
    if (read["subject_hash"], read["access_id"]) != (update["control"]["subject_hash"], update["control"]["access_id"]):
        raise DecisionRefused("card_plan_reset_control_moved")
    try:
        fields, _display, digest = managed_card_reset_display(
            original, card_authority_from_record(control), resource=update["resource"])
    except ManagedCardResetError as exc:
        raise DecisionRefused("card_plan_reset_unchanged") from exc
    except CallerWriteRefused as exc:
        raise DecisionRefused("card_plan_reset_not_held") from exc
    if digest != update["display_digest"]:
        raise DecisionRefused("card_plan_reset_display_moved")
    candidate = CardAuthority.from_mapping({**original.to_dict(), **{field: fields[field] for field in RESET_FIELDS},
                                            "card_revision": original.card_revision + 1})
    if dataclasses.replace(candidate, card_revision=original.card_revision) == original:
        raise DecisionRefused("card_plan_reset_unchanged")
    # ``read``: C's exact link, which STAGE fences under C's lock through PUBLISH (store read member).
    return {"member": group_member(original=original, candidate=candidate, action="update"), "read": read}


__all__ = ["RESET_KEYS", "build_reset_to_control_update", "reset_update_shape_valid"]

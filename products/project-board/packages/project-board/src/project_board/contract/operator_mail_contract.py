from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import DomainError


OPERATOR_MAIL_KINDS = frozenset(
    {
        "question",
        "blocked",
        "decision",
        "delivery_failed",
        "progress",
        "reply",
        "update",
        "result",
    }
)
OPERATOR_NOTIFY_KINDS = frozenset(
    {"question", "blocked", "decision", "delivery_failed"}
)
OPERATOR_RECIPIENTS = frozenset({"operator", "owner"})
# W313 step 5: the project's acting coordinator, whoever holds the role when
# the mail is sent. The board resolves it; a client never resolves it locally.
COORDINATOR_RECIPIENT = "coordinator"
OPERATOR_CHANNELS = frozenset({"board", "telegram", "unknown"})


def safe_operator_origin(value: Any) -> dict[str, str]:
    """The one public origin shape; routes and person/Telegram IDs stay private.

    An old envelope or a malformed handle is unknown, never inferred from a
    subject, claimed payload channel or matching historical correlation.
    """
    if isinstance(value, Mapping):
        channel = str(value.get("channel") or "unknown")
        ref = str(value.get("ref") or "")
        if channel in {"board", "telegram"} and re.fullmatch(r"origin_[0-9a-f]{32}", ref):
            return {"ref": ref, "channel": channel}
    return {"ref": "", "channel": "unknown"}


def require_operator_mail_kind(kind: str) -> str:
    """Return one admitted operator-mail kind or name the rejected value."""

    normalized = str(kind or "").strip()
    if normalized not in OPERATOR_MAIL_KINDS:
        allowed = sorted(OPERATOR_MAIL_KINDS)
        raise DomainError(
            "work_mail_kind_invalid",
            f"Mail kind {normalized!r} is invalid for the operator. "
            f"Valid kinds: {', '.join(allowed)}.",
            details={
                "field": "kind",
                "value": normalized,
                "kind": normalized,
                "allowed": allowed,
            },
        )
    return normalized


__all__ = [
    "COORDINATOR_RECIPIENT",
    "OPERATOR_MAIL_KINDS",
    "OPERATOR_NOTIFY_KINDS",
    "OPERATOR_RECIPIENTS",
    "OPERATOR_CHANNELS",
    "safe_operator_origin",
    "require_operator_mail_kind",
]

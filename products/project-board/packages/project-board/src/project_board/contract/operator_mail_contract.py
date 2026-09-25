from __future__ import annotations

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
    "require_operator_mail_kind",
]

from __future__ import annotations

from typing import Any, Mapping

from .errors import DomainError


REFUSED_OUTCOME_STATES = frozenset({"denied", "not_applied", "refused", "rejected"})
MIXED_OUTCOME_STATES = frozenset({"mixed", "partial", "partially_applied"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _transport_status(outcome: Mapping[str, Any], code: str) -> int:
    for field in ("transport_status", "status_code", "http_status"):
        try:
            status = int(outcome.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if 400 <= status <= 599:
            return status
    if "unavailable" in code:
        return 503
    if any(
        marker in code
        for marker in (
            "authorization",
            "card_required",
            "denied",
            "forbidden",
            "grant_required",
            "operation_required",
            "operator_required",
            "self_forbidden",
        )
    ):
        return 403
    return 409


def _outcome_details(
    operation: str,
    outcome: Mapping[str, Any],
    *,
    state: str,
    error: Mapping[str, Any],
) -> dict[str, Any]:
    details = _mapping(error.get("details"))
    details.update(_mapping(outcome.get("details")))
    for field in (
        "decision",
        "disposition",
        "expected_revision",
        "field",
        "item_ref",
        "missing_field",
        "missing_grant",
        "missing_grants",
        "observed_revision",
        "required_grant",
        "required_operation",
        "required_reviewer",
        "required_resource",
        "work_ref",
    ):
        if field in outcome and field not in details:
            details[field] = outcome[field]
    details.setdefault("operation", str(outcome.get("operation") or operation))
    details.setdefault("outcome_state", state)
    if outcome.get("applied") is False:
        details.setdefault("applied", False)
    return details


def require_applied_operation_outcome(
    operation: str,
    outcome: Any,
) -> Any:
    """Refuse a transport-success envelope for an outcome that did not apply.

    Domain services may return a refusal after transactionally recording its
    idempotency receipt. Transport adapters call this function after that
    transaction completes so the durable receipt remains while the caller sees
    a failed operation.
    """

    if not isinstance(outcome, Mapping):
        return outcome
    value = dict(outcome)
    state = str(value.get("state") or value.get("outcome") or "").strip().lower()
    error = _mapping(value.get("error"))

    if state in MIXED_OUTCOME_STATES or value.get("mixed") is True:
        details = _outcome_details(
            operation,
            value,
            state=state or "mixed",
            error=error,
        )
        reason = str(value.get("reason") or error.get("code") or "").strip()
        if reason:
            details.setdefault("outcome_reason", reason)
        raise DomainError(
            "work_operation_mixed",
            str(
                value.get("summary")
                or error.get("message")
                or value.get("message")
                or "The operation produced a mixed outcome."
            ),
            status=409,
            details=details,
        )

    if value.get("applied") is True:
        # The receipt says the mutation applied. Its ``state`` is then the
        # object's own state, not the outcome: an assignment report with
        # state ``refused`` is a report that applied and set the assignment
        # to refused (2026-09-23, W284: the worker saw ERROR
        # work_operation_refused for a report the service had applied, and
        # its identical retry was refused instead of shown as a replay).
        return outcome
    if state not in REFUSED_OUTCOME_STATES and value.get("applied") is not False:
        return outcome

    code = str(
        value.get("reason")
        or error.get("code")
        or value.get("code")
        or "work_operation_refused"
    ).strip()
    summary = str(
        value.get("summary")
        or error.get("message")
        or value.get("message")
        or "Problem Board refused the operation."
    )
    raise DomainError(
        code,
        summary,
        status=_transport_status(value, code),
        details=_outcome_details(
            operation,
            value,
            state=state or "refused",
            error=error,
        ),
    )


def require_successful_operation_envelope(
    operation: str,
    envelope: Any,
) -> Any:
    """Defend clients that receive an older successful outer envelope."""

    if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
        return envelope
    action = str(envelope.get("operation") or operation)
    for field in ("object", "result"):
        nested = envelope.get(field)
        if isinstance(nested, Mapping):
            require_applied_operation_outcome(action, nested)
            break
    return envelope


__all__ = [
    "MIXED_OUTCOME_STATES",
    "REFUSED_OUTCOME_STATES",
    "require_applied_operation_outcome",
    "require_successful_operation_envelope",
]
